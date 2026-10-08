"""
TLS crypto modes — what "classical / hybrid / pqc" mean on the wire, and how
to prove which one a connection actually used.

Track B (crypto). Phase 4 of the Python 3.14 plan.

--------------------------------------------------------------------
THE THREE MODES

    mode       TLS key exchange    certificates    config file
    classical  X25519              ECDSA P-256     tls/classical.cnf
    hybrid     X25519MLKEM768      ECDSA P-256     tls/hybrid.cnf
    pqc        MLKEM768            ML-DSA-44       tls/pqc.cnf   (+ --cert-dir certs/pqc)
    (CSMS)     offers all three    ECDSA + ML-DSA  tls/server.cnf (+ --pq-cert-dir certs/pqc)
    (rotating charger)  classical until its certificate is rotated, then pqc,
               in ONE process: tls/rotation.cnf (Phase 5)

WHY A CONFIG FILE AND NOT AN API CALL
  Python 3.14's ssl module has no call to choose TLS 1.3 key-exchange groups
  (set_ecdh_curve accepts only classical curves) and none to choose signature
  algorithms. OpenSSL takes both from the file named by the OPENSSL_CONF
  environment variable, read ONCE when the process first loads ssl. So the
  mode is a per-process setting: start the server and each charger process
  with the right OPENSSL_CONF. This module does not set it -- it checks it,
  so a run in the wrong mode is reported instead of measured.

ONE SERVER FOR A MIXED FLEET (Phase 4b, verified)
  The CSMS trusts both roots and holds two identities. Which SERVER certificate
  a charger sees is chosen by the name it asks for (TLS SNI):
    "localhost"     -> ECDSA     (classical and hybrid chargers, legacy chargers)
    PQ_SERVER_NAME  -> ML-DSA-44 (pqc chargers, and rotated chargers)
  So a classical charger is fully classical even when its OpenSSL could verify
  ML-DSA -- which a rotating charger's must, for after its switch. Each
  charger's group list decides the key exchange; classical contexts pin X25519
  with pin_classical_key_exchange(), so they stay classical in a process whose
  group list also allows ML-KEM (tls/rotation.cnf).

PROVING THE NEGOTIATED GROUP
  Python 3.14 has no SSLSocket.group(). install_group_probe() hooks the
  context's handshake-message callback (CPython's _msg_callback, a debug hook)
  to read the key_share group out of the ServerHello. It only OBSERVES; it
  never changes the handshake. negotiated_group(ssl_object) returns the name.
"""

from __future__ import annotations

import os
import re
import ssl
from pathlib import Path
from typing import Any

MODES = ("classical", "hybrid", "pqc")

GROUP_NAMES: dict[int, str] = {
    0x001D: "x25519",
    0x0017: "secp256r1",
    0x0018: "secp384r1",
    0x11EB: "SecP256r1MLKEM768",
    0x11EC: "X25519MLKEM768",
    0x11ED: "SecP384r1MLKEM1024",
    0x0200: "MLKEM512",
    0x0201: "MLKEM768",
    0x0202: "MLKEM1024",
}

EXPECTED_GROUP = {"classical": "x25519", "hybrid": "X25519MLKEM768", "pqc": "MLKEM768"}
EXPECTED_CERTIFICATE = {"classical": "ECDSA-P256", "hybrid": "ECDSA-P256", "pqc": "ML-DSA-44"}

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TLS_CONFIG_DIR = PROJECT_ROOT / "tls"
MODE_CONFIG = {mode: TLS_CONFIG_DIR / f"{mode}.cnf" for mode in MODES}
SERVER_CONFIG = TLS_CONFIG_DIR / "server.cnf"
ROTATION_CONFIG = TLS_CONFIG_DIR / "rotation.cnf"

PQ_SERVER_NAME = "pq.localhost"
"""The TLS server name (SNI) under which the CSMS presents its ML-DSA identity.
It must be in the ML-DSA server certificate's SAN (bootstrap_pki puts it
there). Chargers still dial the CSMS's real address; only the SNI / hostname
check uses this name."""

_PROBE_ATTR = "_pqcharge_group_probe"
_KEY_SHARE_EXTENSION = 0x0033


# -- reading the negotiated group ---------------------------------------

def parse_server_hello_group(handshake: bytes) -> int | None:
    """
    The key_share group id in a TLS 1.3 ServerHello handshake message
    (including its 4-byte handshake header). None if absent or malformed.
    """
    try:
        if not handshake or handshake[0] != 2:  # 2 = ServerHello
            return None
        p = 4 + 2 + 32                           # header, legacy_version, random
        p += 1 + handshake[p]                    # legacy_session_id
        p += 2 + 1                               # cipher_suite, compression
        end = p + 2 + int.from_bytes(handshake[p:p + 2], "big")
        p += 2
        while p + 4 <= end:
            ext_type = int.from_bytes(handshake[p:p + 2], "big")
            ext_len = int.from_bytes(handshake[p + 2:p + 4], "big")
            if ext_type == _KEY_SHARE_EXTENSION and ext_len >= 2:
                return int.from_bytes(handshake[p + 4:p + 6], "big")
            p += 4 + ext_len
    except IndexError:
        return None
    return None


def group_name(group_id: int | None) -> str | None:
    if group_id is None:
        return None
    return GROUP_NAMES.get(group_id, f"0x{group_id:04x}")


def install_group_probe(context: ssl.SSLContext) -> ssl.SSLContext:
    """
    Record the negotiated key-exchange group of every connection made with
    this context (server or client side). Returns the same context.
    Safe to call more than once.
    """
    if getattr(context, _PROBE_ATTR, None) is not None:
        return context
    groups: dict[int, int | None] = {}

    def _callback(conn: Any, direction: str, version: Any, content_type: Any,
                  msg_type: Any, data: bytes) -> None:
        if (content_type == ssl._TLSContentType.HANDSHAKE
                and msg_type == ssl._TLSMessageType.SERVER_HELLO):
            groups[id(conn)] = parse_server_hello_group(data)

    setattr(context, _PROBE_ATTR, groups)
    context._msg_callback = _callback
    return context


def negotiated_group(ssl_object: Any) -> str | None:
    """Group name for a finished handshake (SSLObject or SSLSocket), or None
    when the context had no probe or the handshake was not seen. Reading it
    releases the stored entry."""
    if ssl_object is None:
        return None
    context = getattr(ssl_object, "context", None)
    groups = getattr(context, _PROBE_ATTR, None)
    if groups is None:
        return None
    for candidate in (ssl_object, getattr(ssl_object, "_sslobj", None)):
        if candidate is not None and id(candidate) in groups:
            return group_name(groups.pop(id(candidate)))
    return None


def certificate_key_type(certificate_der: bytes | None) -> str | None:
    """'ECDSA-P256', 'ML-DSA-44', ... for a DER certificate; None if absent."""
    if not certificate_der:
        return None
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives.asymmetric import ec

        key = x509.load_der_x509_certificate(certificate_der).public_key()
        if isinstance(key, ec.EllipticCurvePublicKey):
            return {"secp256r1": "ECDSA-P256", "secp384r1": "ECDSA-P384"}.get(
                key.curve.name, f"ECDSA-{key.curve.name}")
        try:
            from cryptography.hazmat.primitives.asymmetric import mldsa

            for name in ("44", "65", "87"):
                if isinstance(key, getattr(mldsa, f"MLDSA{name}PublicKey")):
                    return f"ML-DSA-{name}"
        except ImportError:
            pass
        return type(key).__name__
    except Exception:  # noqa: BLE001 - a label, never a crash
        return None


def pin_classical_key_exchange(context: ssl.SSLContext) -> ssl.SSLContext:
    """Restrict one context to X25519, whatever the process group list says.
    (set_ecdh_curve accepts classical curves only, which is exactly enough to
    keep a classical charger classical inside a rotation process.)"""
    context.set_ecdh_curve("X25519")
    return context


def server_name_for(key_type: str | None) -> str | None:
    """The SNI a charger presenting this kind of certificate should send:
    PQ_SERVER_NAME for an ML-DSA charger certificate, None (the URL host)
    otherwise."""
    return PQ_SERVER_NAME if key_type and key_type.startswith("ML-DSA") else None


# -- checking the process configuration ---------------------------------

def configured_groups(env: dict[str, str] | None = None) -> list[str] | None:
    """The Groups list from the file OPENSSL_CONF names, or None when
    OPENSSL_CONF is unset, unreadable, or has no Groups line (OpenSSL
    defaults then apply: X25519MLKEM768 first, no pure MLKEM768)."""
    env = os.environ if env is None else env
    path = env.get("OPENSSL_CONF")
    if not path:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r"^\s*Groups\s*=\s*(\S+)\s*$", text, re.MULTILINE)
    return match.group(1).split(":") if match else None


def check_process_mode(mode: str | None, *, server: bool = False,
                       env: dict[str, str] | None = None) -> str | None:
    """
    A warning message if this process's OpenSSL configuration cannot produce
    `mode` (or, for the server, cannot accept every mode). None when fine.
    """
    tls_ok = ssl.OPENSSL_VERSION_INFO >= (3, 5)
    if not tls_ok:
        return (f"this Python uses {ssl.OPENSSL_VERSION}; post-quantum TLS needs "
                f"OpenSSL >= 3.5 (Python 3.14). Every connection will be classical.")
    groups = configured_groups(env)
    if server:
        if groups is None or not {"X25519", "X25519MLKEM768", "MLKEM768"} <= set(groups):
            return (f"server OPENSSL_CONF does not offer X25519, X25519MLKEM768 and "
                    f"MLKEM768 (groups={groups or 'OpenSSL default'}); pqc chargers "
                    f"will fail with NO_SUITABLE_KEY_SHARE. Start the CSMS with "
                    f"OPENSSL_CONF={SERVER_CONFIG}")
        return None
    if mode is None or mode not in MODES:
        return None
    wanted = {"classical": "X25519", "hybrid": "X25519MLKEM768", "pqc": "MLKEM768"}[mode]
    if mode == "classical" and groups is not None and "X25519" in groups:
        return None  # classical contexts pin X25519 themselves (pin_classical_key_exchange)
    if groups is None or groups[0] != wanted:
        return (f"crypto mode {mode!r} but OPENSSL_CONF groups are "
                f"{groups or 'OpenSSL default'}; this charger will NOT negotiate "
                f"{EXPECTED_GROUP[mode]}. Start it with OPENSSL_CONF={MODE_CONFIG[mode]}")
    return None
