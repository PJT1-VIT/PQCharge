"""
The PQC migration wire protocol — Option B, carried over OCPP DataTransfer.

Track C (agent). Phase C8 — Track B integration.

--------------------------------------------------------------------
WHY THIS FILE EXISTS, AND WHY IT IS THE ONE SOURCE OF TRUTH

Track B chose Option B: a station proves its post-quantum identity by
signing a server challenge with an ML-DSA key, not by presenting a PQC
certificate. TLS stays classical; OCPP's standard message *types* are
untouched. But new exchanges still have to cross the wire:

    1. ENROLMENT (Contract 7, C-P1) the server asks a station to make its
                 own ML-DSA key pair; the station keeps the private key and
                 answers with the PUBLIC key only.
    2. CHALLENGE the server sends a nonce; the station signs it.
    3. INSTALL   (DEPRECATED, Contract 7 section 7.8) the orchestrator
                 gives a station a private key it generated. Kept only
                 until Track B's orchestrator and Track A's wiring switch
                 to ENROLMENT; then removed from both sides (closes L04:
                 a private key on the wire ends up in server logs).

OCPP 2.0.1 has no native message for either. Its designed extension
point is DataTransfer -- a generic (vendorId, messageId, data) envelope
that a conformant server and client may use for exactly this. So both
exchanges ride on DataTransfer, and a stock third-party OCPP client
(E6) that has never heard of PQCharge simply answers UnknownVendorId
and carries on. That property -- PQC being additive, not a hard gate --
is what made Option B the choice.

*** THIS MODULE IS IMPORTED BY BOTH SIDES. ***

    agent/client.py         parses these messages when they arrive
    the orchestrator wiring  builds the messages from the build_* functions

Contract 7 (PQCharge_Interface_Contracts.md, section 7.2) is the written
specification of every message here. Change this file only together with
that contract, and tell Tracks A and B first.

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
import hashlib
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
"""DEPRECATED (Contract 7 section 7.8): carries a private key. Removed once
the orchestrator uses MSG_REQUEST_ENROLMENT."""

MSG_CHALLENGE = "PQAuthChallenge"

MSG_REQUEST_ENROLMENT = "RequestPQEnrolment"
"""Contract 7 section 7.2: server -> station, 'make your own key pair and
send me the public key'. Replaces MSG_INSTALL."""

KEY_ID_HEX_CHARS = 16
"""Length of a key_id: the first 16 hex characters (64 bits) of the SHA-256
of the public key. Enough to tell a station's current and previous keys
apart and to correlate log lines; it is an identifier, not a security
check -- verification always uses the full enrolled public key."""

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
    *** DEPRECATED (Contract 7 section 7.8) -- use build_enrolment_request. ***
    Kept, unchanged, until Track B's orchestrator and Track A's wiring use
    the enrolment flow; then removed from both sides.

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
# ENROLMENT — Contract 7 section 7.2 (C-P1)
# =====================================================================
#
#   server  --RequestPQEnrolment {algorithm}-->                   station
#   server  <--Accepted {algorithm, public_key, key_id}--          station
#
# The station generates the key pair, stores it (Contract 7 section 7.3)
# BEFORE replying, and never sends the private key. The server enrols the
# public key and then challenges the station to prove it holds the match.


def key_id_for(public_key: bytes) -> str:
    """
    The key_id of a public key: first KEY_ID_HEX_CHARS hex characters of
    its SHA-256. One definition, used by the station (in its reply and its
    key file), the orchestrator and the boot verifier (in challenges and
    events), and the analysis -- so the same key has the same id everywhere.

    Raises ValueError on empty input: an empty key is a bug upstream, and an
    id for it would look valid in every log.
    """
    raw = bytes(public_key)
    if not raw:
        raise ValueError("cannot compute key_id of an empty public key")
    return hashlib.sha256(raw).hexdigest()[:KEY_ID_HEX_CHARS]


def build_enrolment_request(
    station_id: str,
    *,
    algorithm: str = DEFAULT_ALGORITHM,
) -> Any:
    """
    *** THIS IS THE ORCHESTRATOR'S `enrolment_request_factory`. ***
    (Contract 7 section 7.4; replaces build_install_message.)

    Called as factory(station_id). station_id is accepted for symmetry and
    logging only; the payload does not carry it, because the dispatcher
    already addresses the message to that station.

    Carries NO key material: the whole point of Contract 7 is that the
    private key is made on, and never leaves, the station.
    """
    if not isinstance(algorithm, str) or not algorithm:
        raise ValueError("algorithm must be a non-empty string")
    return call.DataTransfer(
        vendor_id=PQC_VENDOR_ID,
        message_id=MSG_REQUEST_ENROLMENT,
        data=json.dumps({"algorithm": algorithm}),
    )


def parse_enrolment_request(data: Any) -> str:
    """
    Station side. Returns the requested algorithm.

    Raises ValueError on any malformation; the agent's handler turns that
    into a Rejected DataTransfer, never a CALLError. Whether the station
    SUPPORTS the algorithm is the handler's decision, not the parser's.
    """
    obj = _loads(data)
    algorithm = obj.get("algorithm")
    if not isinstance(algorithm, str) or not algorithm:
        raise ValueError("RequestPQEnrolment payload missing 'algorithm'")
    return algorithm


def pack_enrolment_reply(algorithm: str, public_key: bytes) -> str:
    """
    Station side: the DataTransferResponse.data for an Accepted enrolment.

    The key_id is computed here, from the public key, so the station cannot
    send an id that does not match its key.
    """
    if not isinstance(algorithm, str) or not algorithm:
        raise ValueError("algorithm must be a non-empty string")
    raw = bytes(public_key)
    return json.dumps(
        {
            "algorithm": algorithm,
            "public_key": base64.b64encode(raw).decode("ascii"),
            "key_id": key_id_for(raw),
        }
    )


def parse_enrolment_reply(data: Any) -> tuple[str, bytes, str]:
    """
    *** THIS IS THE ORCHESTRATOR'S `public_key_parser` (applied to the
    response's .data). *** Returns (algorithm, public_key, key_id).

    Raises ValueError if anything is missing or malformed, if the reply
    carries a private key (a station must never send one -- refusing it
    keeps an old or buggy station from putting key material into the
    server's logs), or if key_id does not match the public key. The
    orchestrator treats any exception as a failed station, not a crashed
    wave (Contract 7 section 7.4).
    """
    obj = _loads(data)
    if "private_key" in obj:
        raise ValueError("enrolment reply must not contain a private key")
    algorithm = obj.get("algorithm")
    if not isinstance(algorithm, str) or not algorithm:
        raise ValueError("enrolment reply missing 'algorithm'")
    public_key = _b64decode(obj.get("public_key"), "public_key")
    key_id = obj.get("key_id")
    expected = key_id_for(public_key)
    if key_id != expected:
        raise ValueError(
            f"enrolment reply key_id {key_id!r} does not match its public key "
            f"(expected {expected!r})"
        )
    return algorithm, public_key, key_id


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

    Contract 7 section 7.2: pass key_id=key_id_for(enrolled_public_key) so
    the station signs with that key (its current or previous one). Without
    key_id the station signs with its current key.
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


def challenge_key_id(payload: dict) -> str | None:
    """
    Station side: the key_id a challenge asks for, or None if it names none
    (Contract 7 section 7.2). Takes the payload returned by
    parse_challenge_data. Raises ValueError if key_id is present but not a
    string of KEY_ID_HEX_CHARS hex characters.
    """
    key_id = payload.get("key_id")
    if key_id is None:
        return None
    if (not isinstance(key_id, str) or len(key_id) != KEY_ID_HEX_CHARS
            or any(c not in "0123456789abcdef" for c in key_id)):
        raise ValueError(f"challenge key_id {key_id!r} is not {KEY_ID_HEX_CHARS} lowercase hex characters")
    return key_id


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