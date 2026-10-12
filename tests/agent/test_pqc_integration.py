"""
Track B integration — the agent answers the migration protocol.

Track C (tests). Phase C8.

--------------------------------------------------------------------
WHAT THESE PROVE

The agent's half of Contract 7, exercised through the REAL agent path --
the DataTransfer handler in agent/client.py, the enrolment and challenge
handlers in agent/station.py, and agent/pq_identity.py -- against REAL
ML-DSA from crypto/pq.py and Track B's own crypto/pq_auth.py. (Before
2026-10-12 these tests drove the old InstallPQAuth message, now removed
from every track, L42; the same properties are proven with enrolment.)

The decisive test is test_a_migrated_station_authenticates_for_real: it
reproduces the harness's scenario 5, but the signing happens inside the
agent's handler rather than in a one-line script. If the server's
verify_response returns True over a signature the agent produced from a
DataTransfer it parsed, the two halves fit.

These need the quantcrypt wheel (crypto/pq.py) and crypto/pq_auth.py. If
either is absent the module skips, so a machine without the PQ backend
still runs the rest of the suite.
"""

from __future__ import annotations

import pytest

# The PQ backend and Track B's authenticator. Skip cleanly if either is
# not present -- the same importorskip discipline Track B put on its own
# PQ tests, so the suite stays green on a machine without the wheel.
pytest.importorskip("quantcrypt", reason="PQ backend; see requirements.txt")
pq = pytest.importorskip("crypto.pq", reason="Track B crypto/pq.py")
pq_auth = pytest.importorskip("crypto.pq_auth", reason="Track B crypto/pq_auth.py")

from agent import pqc_messages as pqcm  # noqa: E402
from agent.pq_identity import PQIdentity  # noqa: E402
from agent.station import ChargingStation  # noqa: E402
from agent.config import AgentConfig  # noqa: E402


def make_station(station_id: str = "CP0001") -> ChargingStation:
    return ChargingStation(
        AgentConfig(station_id=station_id, csms_url="ws://localhost:9000")
    )


def make_client(station: ChargingStation):
    from agent.client import StationClient

    return StationClient(station.config.station_id, connection=None, commands=station)


async def send(client, message):
    """Deliver one server DataTransfer through the agent's real handler."""
    return await client.on_data_transfer(
        vendor_id=message.vendor_id, message_id=message.message_id, data=message.data,
    )


async def enrol(client, authenticator=None):
    """Contract 7 section 7.4: RequestPQEnrolment -> the station makes its own
    key and answers with the PUBLIC key; the server (if given) enrols it."""
    response = await send(client, pqcm.build_enrolment_request("CP0001"))
    assert response.status == pqcm.STATUS_ACCEPTED
    algorithm, public_key, key_id = pqcm.parse_enrolment_reply(response.data)
    if authenticator is not None:
        authenticator.enrol("CP0001", public_key)
    return algorithm, public_key, key_id


# =====================================================================
# THE WIRE — one definition, both sides
# =====================================================================


def test_challenge_round_trips():
    nonce = b"\x00\x01\x02\x03thirty-two-bytes-of-nonce-here!"
    message = pqcm.build_challenge_message(nonce)
    parsed_nonce, _payload = pqcm.parse_challenge_data(message.data)
    assert parsed_nonce == nonce


# =====================================================================
# THE HANDLER — enrolment (Contract 7 section 7.2)
# =====================================================================


@pytest.mark.asyncio
async def test_enrolment_migrates_the_station_and_sends_only_the_public_key():
    """
    A DataTransfer RequestPQEnrolment arrives; the station makes its own
    key pair, reports itself migrated and answers Accepted with the PUBLIC
    key and its key_id -- never the private key (L01).
    """
    station = make_station()
    client = make_client(station)

    assert station.pq.is_migrated is False
    response = await send(client, pqcm.build_enrolment_request("CP0001"))
    assert response.status == pqcm.STATUS_ACCEPTED
    assert "private_key" not in str(response.data)
    algorithm, public_key, key_id = pqcm.parse_enrolment_reply(response.data)
    assert station.pq.is_migrated is True
    assert algorithm == station.pq.algorithm == "ML-DSA-44"
    assert key_id == station.pq.key_id == pqcm.key_id_for(public_key)


@pytest.mark.asyncio
async def test_a_migrated_station_authenticates_for_real():
    """
    *** THE test in this file — the harness's scenario 5, through the
    agent's own handlers. ***

    Server (Track B's real PQAuthenticator) asks the station for its key,
    enrols the PUBLIC key it answers with, then challenges it. The
    signature the agent produces must satisfy verify_response. Nothing is
    faked but the socket.
    """
    station = make_station("CP0001")
    client = make_client(station)

    provider = pq.PQProvider()
    authenticator = pq_auth.PQAuthenticator(provider)

    # 1. Enrolment: the station makes the key; the server enrols its public half.
    _alg, _public_key, key_id = await enrol(client, authenticator)

    # 2. The server issues a challenge naming that key and sends it down.
    nonce = authenticator.issue_challenge("CP0001")
    response = await send(client, pqcm.build_challenge_message(nonce, key_id=key_id))
    assert response.status == pqcm.STATUS_ACCEPTED

    # 3. The server verifies the agent's signature against the enrolled
    #    public key. This is the whole point.
    signature = pqcm.parse_signature(response.data)
    assert authenticator.verify_response("CP0001", signature) is True


@pytest.mark.asyncio
async def test_an_impostor_signature_is_rejected_by_the_server():
    """A station holding a DIFFERENT key cannot answer for CP0001. This is
    the E5 property: the enrolled key is what binds the identity."""
    station = make_station("CP0001")
    client = make_client(station)

    provider = pq.PQProvider()
    authenticator = pq_auth.PQAuthenticator(provider)

    # The station makes and keeps its own key ...
    await enrol(client)
    # ... but the server holds a DIFFERENT public key for CP0001.
    _other_priv, other_pub = provider.generate_keypair()
    authenticator.enrol("CP0001", other_pub)

    nonce = authenticator.issue_challenge("CP0001")
    response = await send(client, pqcm.build_challenge_message(nonce))
    # The agent signs happily -- it does not know the server's key differs --
    # but the server rejects the signature.
    signature = pqcm.parse_signature(response.data)
    assert authenticator.verify_response("CP0001", signature) is False


# =====================================================================
# THE HANDLER — refusals, all in valid OCPP
# =====================================================================


@pytest.mark.asyncio
async def test_a_challenge_before_migration_is_rejected():
    """Honest answer: a station with no key cannot sign, and says so,
    rather than pretending or crashing."""
    client = make_client(make_station())
    challenge = pqcm.build_challenge_message(b"some-nonce-bytes-00000000000000")
    result = await send(client, challenge)
    assert result.status == pqcm.STATUS_REJECTED


@pytest.mark.asyncio
async def test_a_malformed_enrolment_request_is_rejected_not_crashed():
    """A bad payload must be a Rejected DataTransfer, never a CALLError --
    which would brand the station faulty when the message was bad."""
    station = make_station()
    result = await make_client(station).on_data_transfer(
        vendor_id=pqcm.PQC_VENDOR_ID,
        message_id=pqcm.MSG_REQUEST_ENROLMENT,
        data="not json at all",
    )
    assert result.status == pqcm.STATUS_REJECTED
    assert station.pq.is_migrated is False


@pytest.mark.asyncio
async def test_the_removed_install_message_is_answered_unknown_message():
    """L42: InstallPQAuth (a server-made PRIVATE key) was removed from every
    track on 2026-10-12. If one ever arrives it is an unknown message id --
    answered as such, and no key is taken."""
    station = make_station()
    result = await make_client(station).on_data_transfer(
        vendor_id=pqcm.PQC_VENDOR_ID,
        message_id="InstallPQAuth",
        data='{"algorithm": "ML-DSA-44", "private_key": "AAAA"}',
    )
    assert result.status == pqcm.STATUS_UNKNOWN_MESSAGE
    assert station.pq.is_migrated is False


@pytest.mark.asyncio
async def test_a_foreign_vendor_is_answered_unknown_vendor():
    """
    A stock third-party OCPP client (E6) sends its own DataTransfer and
    must not be disturbed by a migration protocol it never heard of.
    """
    result = await make_client(make_station()).on_data_transfer(
        vendor_id="some.other.vendor", message_id="Whatever", data="{}"
    )
    assert result.status == pqcm.STATUS_UNKNOWN_VENDOR


@pytest.mark.asyncio
async def test_an_unknown_message_id_is_answered_unknown_message():
    result = await make_client(make_station()).on_data_transfer(
        vendor_id=pqcm.PQC_VENDOR_ID, message_id="NotAThing", data="{}"
    )
    assert result.status == pqcm.STATUS_UNKNOWN_MESSAGE


# =====================================================================
# ROTATION — a new enrolment replaces the key, identity unchanged
# =====================================================================


def test_enrolling_again_rotates_the_key_and_keeps_the_previous_one():
    """
    Track B rotates a station (B-F2) by asking it to enrol again. The station
    makes a new key, keeps the old one as "previous" (so a server that has
    not committed the new key yet can still check it), and its id -- its
    identity -- never changes: the impersonation invariant.
    """
    identity = PQIdentity("CP0001", provider=pq.PQProvider())
    _pub1, k1 = identity.enrol("ML-DSA-44")
    _pub2, k2 = identity.enrol("ML-DSA-44")  # rotation
    assert k1 != k2
    assert identity.key_id == k2 and identity.previous_key_id == k1
    assert identity.is_migrated and identity.station_id == "CP0001"
