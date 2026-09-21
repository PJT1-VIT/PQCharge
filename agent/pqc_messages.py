"""
The PQC migration wire protocol — Option B, carried over OCPP DataTransfer.

Track C (agent). Phase C8 — Track B integration.

--------------------------------------------------------------------
WHY THIS FILE EXISTS, AND WHY IT IS THE ONE SOURCE OF TRUTH

Track B chose Option B: a station proves its post-quantum identity by
signing a server challenge with an ML-DSA key, not by presenting a PQC
certificate. TLS stays classical; OCPP's standard message *types* are
untouched. But two new exchanges still have to cross the wire:

    1. INSTALL   the orchestrator gives a station its ML-DSA private key
    2. CHALLENGE the server sends a nonce; the station signs it

OCPP 2.0.1 has no native message for either. Its designed extension
point is DataTransfer -- a generic (vendorId, messageId, data) envelope
that a conformant server and client may use for exactly this. So both
exchanges ride on DataTransfer, and a stock third-party OCPP client
(E6) that has never heard of PQCharge simply answers UnknownVendorId
and carries on. That property -- PQC being additive, not a hard gate --
is what made Option B the choice.

*** THIS MODULE IS IMPORTED BY BOTH SIDES. ***

    agent/client.py         parses these messages when they arrive
    the orchestrator wiring  builds the install message from build_install_message

`build_install_message` IS the `install_message_factory` that Track B's
MigrationOrchestrator takes as a constructor argument (its harness stubs
it as `lambda sid, priv: ("InstallPQAuth", sid)`). Track C provides the
real one, from here, so the bytes the orchestrator sends are the exact
bytes the agent parses. This is the same discipline Track B applied to
`sign_challenge`: one definition of what crosses, shared by both sides,
rather than two hand-written encoders that drift.

--------------------------------------------------------------------
ENCODING

The payload is JSON in DataTransfer.data (a string on the wire). Key and
signature bytes are ML-DSA artifacts -- a 2560-byte private key, a
2420-byte signature -- which are not text, so they are base64-encoded
inside the JSON. base64, not hex, to keep the DataTransfer.data field
small: these ride inside an OCPP message that has a size budget, and the
post-quantum artifacts are already the largest thing on this wire.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from ocpp.v201 import call, call_result

# One vendor id for every PQCharge extension message. A stock OCPP client
# that receives this and does not recognise it answers UnknownVendorId,
# which is correct and harmless -- see the module docstring.
PQC_VENDOR_ID = "pqcharge.pqc"

# The two message ids under that vendor. Plain strings, spelled once
# here, so a typo on either side is a single-file fix rather than a
# silent mismatch that looks like a crypto failure.
MSG_INSTALL = "InstallPQAuth"
MSG_CHALLENGE = "PQAuthChallenge"

DEFAULT_ALGORITHM = "ML-DSA-44"
"""Tagged on every install so a station can refuse a key for an
algorithm it was not built to sign with, rather than producing a
signature nobody can verify."""


def _loads(data: Any) -> dict:
    """
    DataTransfer.data as a dict, whether it arrived as a JSON string or
    was already decoded to a dict by the library or a test.

    Raises ValueError on anything else, so the caller turns a malformed
    payload into a Rejected response rather than a CALLError.
    """
    if isinstance(data, dict):
        return data
    if isinstance(data, str):
        try:
            obj = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ValueError(f"DataTransfer.data was not valid JSON: {exc}") from exc
        if not isinstance(obj, dict):
            raise ValueError("DataTransfer.data JSON was not an object")
        return obj
    raise ValueError(f"DataTransfer.data was {type(data).__name__}, expected str or dict")


def _b64decode(value: Any, field: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a base64 string, got {type(value).__name__}")
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{field} was not valid base64: {exc}") from exc
    if not raw:
        raise ValueError(f"{field} decoded to empty bytes")
    return raw


# =====================================================================
# INSTALL — orchestrator -> station
# =====================================================================


def build_install_message(
    station_id: str,
    private_key: bytes,
    *,
    algorithm: str = DEFAULT_ALGORITHM,
) -> Any:
    """
    *** THIS IS `install_message_factory`. ***

    Track C hands this exact function to Track B's MigrationOrchestrator
    when the orchestrator is wired into the live CSMS:

        MigrationOrchestrator(
            ...,
            install_message_factory=build_install_message,
            ...,
        )

    The orchestrator calls it `(station_id, private_key)` -- matching the
    harness's `lambda sid, priv: (...)` -- and dispatches the result to
    the station through csms.dispatch.CommandDispatcher.send().

    station_id is accepted for symmetry with the factory signature and
    for logging; the payload does not carry it, because the message is
    already addressed to that station by the dispatcher and the station
    knows its own id.

    The public key is NOT sent: the orchestrator has already enrolled it
    in the CSMS authenticator (`enrol(station_id, public_key)`), and the
    station needs only the private key to sign challenges.
    """
    return call.DataTransfer(
        vendor_id=PQC_VENDOR_ID,
        message_id=MSG_INSTALL,
        data=json.dumps(
            {
                "algorithm": algorithm,
                "private_key": base64.b64encode(bytes(private_key)).decode("ascii"),
            }
        ),
    )


def parse_install_data(data: Any) -> tuple[str, bytes]:
    """
    Station side. Returns (algorithm, private_key_bytes).

    Raises ValueError on any malformation. The agent's handler catches
    that and answers a Rejected DataTransfer -- never a CALLError, which
    would tell the CSMS this station is faulty when the truth is the
    message was bad.
    """
    obj = _loads(data)
    algorithm = obj.get("algorithm")
    if not isinstance(algorithm, str) or not algorithm:
        raise ValueError("InstallPQAuth payload missing 'algorithm'")
    private_key = _b64decode(obj.get("private_key"), "private_key")
    return algorithm, private_key


# =====================================================================
# CHALLENGE — server -> station -> server
# =====================================================================


def build_challenge_message(nonce: bytes, **meta: Any) -> Any:
    """
    Build a PQAuthChallenge. Present so a test (or a future server-side
    challenge driver) constructs the same shape the agent parses.

    **meta carries any additional fields Track B's Challenge needs to be
    reconstructed on the station side -- see parse_challenge_data and the
    note in agent/pq_identity.py. Keeping them here means the wire shape
    has one definition, not two.
    """
    payload: dict[str, Any] = {
        "nonce": base64.b64encode(bytes(nonce)).decode("ascii"),
    }
    payload.update(meta)
    return call.DataTransfer(
        vendor_id=PQC_VENDOR_ID,
        message_id=MSG_CHALLENGE,
        data=json.dumps(payload),
    )


def parse_challenge_data(data: Any) -> tuple[bytes, dict]:
    """
    Station side. Returns (nonce_bytes, full_payload).

    The full payload is returned as well as the nonce because Track B's
    `Challenge` may bind more than the nonce (a station id, an algorithm,
    an issued-at time), and the station has to reconstruct whatever
    `sign_challenge` signs. agent/pq_identity.py is where that
    reconstruction happens; this function only decodes the transport.
    """
    obj = _loads(data)
    nonce = _b64decode(obj.get("nonce"), "nonce")
    return nonce, obj


def pack_signature(signature: bytes) -> str:
    """The station's answer, for DataTransferResponse.data."""
    return json.dumps(
        {"signature": base64.b64encode(bytes(signature)).decode("ascii")}
    )


def parse_signature(data: Any) -> bytes:
    """Server side (or a test): pull the signature back out of the response."""
    obj = _loads(data)
    return _b64decode(obj.get("signature"), "signature")


# =====================================================================
# RESPONSE HELPERS — one place that knows the DataTransfer status words
# =====================================================================
#
# DataTransferStatusEnumType is Accepted / Rejected / UnknownMessageId /
# UnknownVendorId. Spelled here so the handler in client.py reads as
# intent, and so a wrong status string fails in one place.

STATUS_ACCEPTED = "Accepted"
STATUS_REJECTED = "Rejected"
STATUS_UNKNOWN_MESSAGE = "UnknownMessageId"
STATUS_UNKNOWN_VENDOR = "UnknownVendorId"