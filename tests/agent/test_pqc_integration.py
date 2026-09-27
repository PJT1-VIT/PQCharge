"""
Track B integration — the agent answers the migration protocol.

Track C (tests). Phase C8.

--------------------------------------------------------------------
WHAT THESE PROVE

Track B's rotation harness ends with one sentence: "Track C's agent must
answer the InstallPQAuth message this harness stubs." These tests are
that answer, exercised through the REAL agent path -- the DataTransfer
handler in agent/client.py, the two handlers in agent/station.py, and
agent/pq_identity.py -- against REAL ML-DSA from crypto/pq.py and Track
B's own crypto/pq_auth.py.

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

from ocpp.v201 import call, call_result  # noqa: E402

from agent import pqc_messages as pqcm  # noqa: E402
from agent.pq_identity import PQIdentity  # noqa: E402
from agent.station import ChargingStation  # noqa: E402
from agent.config import AgentConfig  # noqa: E402


def make_station(station_id: str = "CP0001") -> ChargingStation:
    return ChargingStation(
        AgentConfig(station_id=station_id, csms_url="ws://localhost:9000")
    )


# =====================================================================
# THE WIRE — one definition, both sides
# =====================================================================


def test_install_message_round_trips():
    """
    build_install_message IS the orchestrator's install_message_factory,
    and parse_install_data is the agent reading it. Bytes in must equal
    bytes out, or the key the server generated is not the key the station
    holds.
    """
    provider = pq.PQProvider()
    private_key, _public = provider.generate_keypair()

    message = pqcm.build_install_message("CP0001", private_key)
    assert message.vendor_id == pqcm.PQC_VENDOR_ID
    assert message.message_id == pqcm.MSG_INSTALL

    algorithm, parsed_key = pqcm.parse_install_data(message.data)
    assert parsed_key == private_key
    assert algorithm == "ML-DSA-44"


def test_challenge_round_trips():
    nonce = b"\x00\x01\x02\x03thirty-two-bytes-of-nonce-here!"
    message = pqcm.build_challenge_message(nonce)
    parsed_nonce, _payload = pqcm.parse_challenge_data(message.data)
    assert parsed_nonce == nonce


# =====================================================================
# THE HANDLER — install
# =====================================================================


@pytest.mark.asyncio
async def test_install_migrates_the_station():
    """
    A DataTransfer InstallPQAuth arrives; the station stores the key and
    reports itself migrated, answering an Accepted DataTransfer.
    """
    from agent.client import StationClient

    station = make_station()
    client = StationClient("CP0001", connection=None, commands=station)

    provider = pq.PQProvider()
    private_key, _public = provider.generate_keypair()
    message = pqcm.build_install_message("CP0001", private_key)

    assert station.pq.is_migrated is False
    result = await client.on_data_transfer(
        vendor_id=message.vendor_id,
        message_id=message.message_id,
        data=message.data,
    )
    assert result.status == pqcm.STATUS_ACCEPTED
    assert station.pq.is_migrated is True
    assert station.pq.algorithm == "ML-DSA-44"


@pytest.mark.asyncio
async def test_a_migrated_station_authenticates_for_real():
    """
    *** THE test in this file — the harness's scenario 5, through the
    agent's own handlers. ***

    Server (Track B's real PQAuthenticator + CA-less enrol) enrols the
    public key, installs the private key on the station via the real
    DataTransfer path, then challenges it. The signature the agent
    produces must satisfy verify_response. Nothing is faked but the
    socket.
    """
    from agent.client import StationClient

    station = make_station("CP0001")
    client = StationClient("CP0001", connection=None, commands=station)

    provider = pq.PQProvider()
    authenticator = pq_auth.PQAuthenticator(provider)

    # 1. The orchestrator generates a keypair, enrols the PUBLIC half at
    #    the server, and installs the PRIVATE half on the station.
    private_key, public_key = provider.generate_keypair()
    authenticator.enrol("CP0001", public_key)

    install = pqcm.build_install_message("CP0001", private_key)
    await client.on_data_transfer(
        vendor_id=install.vendor_id,
        message_id=install.message_id,
        data=install.data,
    )
    assert station.pq.is_migrated

    # 2. The server issues a challenge (raw nonce) and sends it down.
    nonce = authenticator.issue_challenge("CP0001")
    challenge = pqcm.build_challenge_message(nonce)
    response = await client.on_data_transfer(
        vendor_id=challenge.vendor_id,
        message_id=challenge.message_id,
        data=challenge.data,
    )
    assert response.status == pqcm.STATUS_ACCEPTED

    # 3. The server verifies the agent's signature against the enrolled
    #    public key. This is the whole point.
    signature = pqcm.parse_signature(response.data)
    assert authenticator.verify_response("CP0001", signature) is True


@pytest.mark.asyncio
async def test_an_impostor_signature_is_rejected_by_the_server():
    """A station holding a DIFFERENT key cannot answer for CP0001. This is
    the E5 property: the enrolled key is what binds the identity."""
    from agent.client import StationClient

    station = make_station("CP0001")
    client = StationClient("CP0001", connection=None, commands=station)

    provider = pq.PQProvider()
    authenticator = pq_auth.PQAuthenticator(provider)

    real_priv, real_pub = provider.generate_keypair()
    wrong_priv, _wrong_pub = provider.generate_keypair()
    authenticator.enrol("CP0001", real_pub)  # server has the REAL public key

    # The station is given the WRONG private key.
    install = pqcm.build_install_message("CP0001", wrong_priv)
    await client.on_data_transfer(
        vendor_id=install.vendor_id, message_id=install.message_id,
        data=install.data,
    )

    nonce = authenticator.issue_challenge("CP0001")
    challenge = pqcm.build_challenge_message(nonce)
    response = await client.on_data_transfer(
        vendor_id=challenge.vendor_id, message_id=challenge.message_id,
        data=challenge.data,
    )
    # The agent signs happily -- it does not know its key is wrong -- but
    # the server rejects the signature.
    signature = pqcm.parse_signature(response.data)
    assert authenticator.verify_response("CP0001", signature) is False


# =====================================================================
# THE HANDLER — refusals, all in valid OCPP
# =====================================================================


@pytest.mark.asyncio
async def test_a_challenge_before_migration_is_rejected():
    """Honest answer: a station with no key cannot sign, and says so,
    rather than pretending or crashing."""
    from agent.client import StationClient

    station = make_station()
    client = StationClient("CP0001", connection=None, commands=station)

    challenge = pqcm.build_challenge_message(b"some-nonce-bytes-00000000000000")
    result = await client.on_data_transfer(
        vendor_id=challenge.vendor_id, message_id=challenge.message_id,
        data=challenge.data,
    )
    assert result.status == pqcm.STATUS_REJECTED


@pytest.mark.asyncio
async def test_a_malformed_install_is_rejected_not_crashed():
    """A bad payload must be a Rejected DataTransfer, never a CALLError --
    which would brand the station faulty when the message was bad."""
    from agent.client import StationClient

    station = make_station()
    client = StationClient("CP0001", connection=None, commands=station)

    result = await client.on_data_transfer(
        vendor_id=pqcm.PQC_VENDOR_ID,
        message_id=pqcm.MSG_INSTALL,
        data='{"algorithm": "ML-DSA-44", "private_key": "not-base64!!"}',
    )
    assert result.status == pqcm.STATUS_REJECTED
    assert station.pq.is_migrated is False


@pytest.mark.asyncio
async def test_a_foreign_vendor_is_answered_unknown_vendor():
    """
    A stock third-party OCPP client (E6) sends its own DataTransfer and
    must not be disturbed by a migration protocol it never heard of.
    """
    from agent.client import StationClient

    station = make_station()
    client = StationClient("CP0001", connection=None, commands=station)

    result = await client.on_data_transfer(
        vendor_id="some.other.vendor", message_id="Whatever", data="{}"
    )
    assert result.status == pqcm.STATUS_UNKNOWN_VENDOR


@pytest.mark.asyncio
async def test_an_unknown_message_id_is_answered_unknown_message():
    from agent.client import StationClient

    station = make_station()
    client = StationClient("CP0001", connection=None, commands=station)

    result = await client.on_data_transfer(
        vendor_id=pqcm.PQC_VENDOR_ID, message_id="NotAThing", data="{}"
    )
    assert result.status == pqcm.STATUS_UNKNOWN_MESSAGE


# =====================================================================
# ROTATION — a re-install replaces the key, transaction untouched
# =====================================================================


def test_reinstalling_rotates_the_key():
    """
    Track B rotates a station by enrolling a fresh key and sending a new
    InstallPQAuth. The station simply holds the newest one. Its id -- its
    identity -- never changes, only the key: the impersonation invariant.
    """
    identity = PQIdentity("CP0001", provider=pq.PQProvider())
    k1, _ = pq.PQProvider().generate_keypair()
    k2, _ = pq.PQProvider().generate_keypair()

    identity.install(k1, "ML-DSA-44")
    assert identity.is_migrated
    identity.install(k2, "ML-DSA-44")  # rotation
    assert identity._private_key == k2  # newest key wins