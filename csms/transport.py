"""
Transport security — OCPP 2.0.1 Security Profile 3 (mutual TLS).

Track A (csms). Day 7, jointly with Track B.

--------------------------------------------------------------------
WHAT THIS IS

The CSMS side of Security Profile 3: both ends present an X.509
certificate and each verifies the other against a shared root.

Track B owns the certificates. `experiments/bootstrap_pki.py` builds a
root CA, a server identity and a certificate per demo station, and
proves the handshake works with real sockets before this module is
involved at all. This file only assembles the ssl.SSLContext the
WebSocket server hands to `serve()`.

Kept out of server.py and free of any websockets or ocpp import, so the
context construction can be unit-tested without a running server -- and
so Day 8's post-quantum swap is a change to what the certificates
contain, not to the server.

--------------------------------------------------------------------
IDENTITY BINDING -- the part that makes Profile 3 mean something

A station announces who it is twice: in the WebSocket path
(ws://host/CP001) and in the Common Name of the certificate it
presents. Nothing in TLS makes those agree.

If they are not checked against each other, any station holding ANY
valid certificate from our CA can claim to be any other station. It
would pass mutual TLS, appear in the registry as its victim, and
receive that station's commands. That is precisely the impersonation
E5 demonstrates -- and it would be available without breaking any
cryptography at all, which would make the experiment's conclusion
wrong.

So the CN is compared against the station id from the path. The check
has three settings because Day 7 is the first time any certificate
reaches this code and a hard failure on day one would be indistinguishable
from a wiring mistake:

    off      do not look
    warn     log a mismatch, allow the connection   (Day 7 default)
    enforce  refuse the connection                  (target before E5)

Track B issues station certificates with common_name = station_id, so
warn is expected to be silent. Once a run confirms that, move to
enforce and record the change.

--------------------------------------------------------------------
CONSEQUENCE FOR THE CONTRACT 6 HTTP SURFACE -- read before enabling TLS

The OCPP endpoint and the /api/ surface share one socket, so turning
TLS on turns it on for both. Two things follow:

  1. /api/fleet becomes https:// and callers need the CA:
         curl --cacert certs/root.pem https://localhost:9000/api/fleet

  2. With client certificates REQUIRED -- which is what Profile 3 means
     -- every TLS connection to that port must present one, including
     the dashboard's polls and any curl. There is no way to exempt the
     HTTP paths: the certificate exchange happens in the handshake,
     before a single byte of HTTP is read.

The principled answer is that the operator console is an identity too,
and Track B issues it a certificate from the same CA. The interim
answer is --tls-client-certs optional, which still encrypts and still
authenticates the server, but stops short of full Profile 3 and must be
recorded as such rather than quietly relied on.
"""

from __future__ import annotations

import logging
import ssl
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("csms.transport")

DEFAULT_CERT_DIR = "certs"
"""Where experiments/bootstrap_pki.py writes. Gitignored; regenerated,
never committed."""

DEFAULT_SERVER_NAME = "localhost"
"""Base name of the CSMS certificate pair, matching
bootstrap_pki.SERVER_COMMON_NAME. The certificate's Subject Alternative
Name currently covers localhost only; when the Raspberry Pi bench node
is integrated the CSMS host's mDNS .local name has to be added, because
the station verifies the server's hostname. Deferred by decision --
which machine hosts the CSMS on the day is not settled."""

CLIENT_CERT_MODES = ("required", "optional", "none")
DEFAULT_CLIENT_CERT_MODE = "required"
"""required is Security Profile 3. Anything else is a documented
deviation, not a configuration preference."""

IDENTITY_CHECK_MODES = ("off", "warn", "enforce")
DEFAULT_IDENTITY_CHECK = "warn"

_VERIFY_MODES = {
    "required": ssl.CERT_REQUIRED,
    "optional": ssl.CERT_OPTIONAL,
    "none": ssl.CERT_NONE,
}


class TlsConfigError(RuntimeError):
    """A TLS setting is wrong or a certificate file is missing.

    Raised at startup rather than at first connection. A CSMS that
    starts with broken TLS and only fails when a station arrives looks
    like a station problem, and during E2 it would look like a
    post-quantum problem.
    """


def default_paths(
    cert_dir: str | Path = DEFAULT_CERT_DIR,
    server_name: str = DEFAULT_SERVER_NAME,
) -> tuple[Path, Path, Path]:
    """The three files bootstrap_pki.py writes for the server side."""
    directory = Path(cert_dir)
    return (
        directory / f"{server_name}.crt.pem",
        directory / f"{server_name}.key.pem",
        directory / "root.pem",
    )


def build_server_context(
    certfile: str | Path,
    keyfile: str | Path,
    cafile: str | Path,
    *,
    client_certs: str = DEFAULT_CLIENT_CERT_MODE,
) -> ssl.SSLContext:
    """
    The CSMS's TLS context.

    Args:
        certfile/keyfile: the CSMS's own identity, from Track B's CA.
        cafile: the root that station certificates are verified against.
        client_certs: 'required' for Security Profile 3.

    Raises:
        TlsConfigError: on an unknown mode, a missing file, or a
            certificate and key that do not belong together.
    """
    if client_certs not in CLIENT_CERT_MODES:
        raise TlsConfigError(
            f"unknown client certificate mode {client_certs!r}; "
            f"expected one of {CLIENT_CERT_MODES}"
        )

    for label, path in (
        ("certificate", certfile),
        ("private key", keyfile),
        ("CA root", cafile),
    ):
        if not Path(path).is_file():
            raise TlsConfigError(
                f"{label} not found: {path}. Run "
                f"`python -m experiments.bootstrap_pki` to generate the PKI."
            )

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        context.load_cert_chain(certfile=str(certfile), keyfile=str(keyfile))
        context.load_verify_locations(cafile=str(cafile))
    except ssl.SSLError as exc:
        raise TlsConfigError(f"could not load TLS material: {exc}") from exc

    context.verify_mode = _VERIFY_MODES[client_certs]

    LOGGER.info(
        "TLS enabled: cert=%s ca=%s client_certs=%s",
        certfile, cafile, client_certs,
    )
    if client_certs != "required":
        LOGGER.warning(
            "client certificates are %s, not required -- this is NOT OCPP "
            "Security Profile 3. Record it in docs/limitations.md if a run "
            "uses it.",
            client_certs,
        )
    return context


def build_client_context(
    certfile: str | Path,
    keyfile: str | Path,
    cafile: str | Path,
    *,
    check_hostname: bool = True,
) -> ssl.SSLContext:
    """
    A station's TLS context. Used by Track A's own test fixture.

    check_hostname stays on by default: the station verifying the
    server's name is half of mutual authentication, and it is the half
    that will fail first when the Raspberry Pi connects to something
    other than localhost. Leaving it on means that failure happens
    where it can be read, rather than being switched off for
    convenience and forgotten.
    """
    for label, path in (
        ("certificate", certfile),
        ("private key", keyfile),
        ("CA root", cafile),
    ):
        if not Path(path).is_file():
            raise TlsConfigError(f"{label} not found: {path}")

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = check_hostname
    try:
        context.load_cert_chain(certfile=str(certfile), keyfile=str(keyfile))
        context.load_verify_locations(cafile=str(cafile))
    except ssl.SSLError as exc:
        raise TlsConfigError(f"could not load TLS material: {exc}") from exc
    return context


def peer_common_name(peer_cert: dict[str, Any] | None) -> str | None:
    """
    Pull the Common Name out of a parsed peer certificate.

    Returns None when there is no certificate or no CN, which the
    caller must treat as "unknown", never as "matches".
    """
    if not peer_cert:
        return None
    for rdn in peer_cert.get("subject", ()):
        for key, value in rdn:
            if key == "commonName":
                return value
    return None


def _ssl_objects(connection: Any) -> list[Any]:
    """
    Every plausible route from a websockets connection to its SSL object.

    Library internals, not a documented API, and the layout has moved
    between releases -- so several routes are tried rather than one. A
    CSMS must not drop a charging station because an attribute moved.
    """
    transports = []
    for holder in (connection, getattr(connection, "protocol", None)):
        if holder is None:
            continue
        for attr in ("transport", "_transport"):
            candidate = getattr(holder, attr, None)
            if candidate is not None and candidate not in transports:
                transports.append(candidate)
    return transports


def peer_certificate(connection: Any) -> dict[str, Any] | None:
    """
    Best-effort read of the parsed peer certificate.

    Tries the asyncio-documented "peercert" key first, then the SSL
    object. A silent None here would mean the identity check quietly
    does nothing, which is worse than it being absent -- so the caller
    logs loudly when it cannot read an identity while TLS is on.
    """
    for transport in _ssl_objects(connection):
        get_extra_info = getattr(transport, "get_extra_info", None)
        if get_extra_info is None:
            continue
        try:
            cert = get_extra_info("peercert")
        except Exception:  # noqa: BLE001 - library internals
            cert = None
        if cert:
            return cert
        try:
            ssl_object = get_extra_info("ssl_object")
        except Exception:  # noqa: BLE001
            ssl_object = None
        if ssl_object is None:
            continue
        try:
            cert = ssl_object.getpeercert()
        except Exception:  # noqa: BLE001
            cert = None
        if cert:
            return cert
    return None


def describe_connection_security(connection: Any) -> dict[str, Any]:
    """
    TLS facts about one live connection, for the event log.

    Returns keys tls_version, tls_cipher, peer_cert_bytes and
    peer_common_name; each is None when unavailable. Empty dict values
    are never invented -- an absent figure must read as absent, not as
    zero, because a zero certificate size would look like a measurement.

    WHY peer_cert_bytes IS WORTH CAPTURING. Experiment E4 asks whether
    post-quantum certificates cross OCPP 2.0.1's documented size limits
    (5,500 bytes for a certificate, 10,000 for a chain). Track B's
    crypto/store.py measures that statically, from certificates on disk.
    This measures the certificate a station ACTUALLY presented on a real
    connection. Two independent sources for the same finding, one of
    them from the running system -- and on Day 8 the same field shows
    the ML-DSA certificate arriving without any extra work.
    """
    facts: dict[str, Any] = {
        "tls_version": None,
        "tls_cipher": None,
        "peer_cert_bytes": None,
        "peer_common_name": None,
    }

    for transport in _ssl_objects(connection):
        get_extra_info = getattr(transport, "get_extra_info", None)
        if get_extra_info is None:
            continue
        try:
            ssl_object = get_extra_info("ssl_object")
        except Exception:  # noqa: BLE001
            ssl_object = None
        if ssl_object is None:
            continue

        try:
            facts["tls_version"] = ssl_object.version()
        except Exception:  # noqa: BLE001
            pass
        try:
            cipher = ssl_object.cipher()
            facts["tls_cipher"] = cipher[0] if cipher else None
        except Exception:  # noqa: BLE001
            pass
        try:
            der = ssl_object.getpeercert(binary_form=True)
            facts["peer_cert_bytes"] = len(der) if der else None
        except Exception:  # noqa: BLE001
            pass
        break

    facts["peer_common_name"] = peer_common_name(peer_certificate(connection))
    return facts


def check_identity(
    station_id: str,
    connection: Any,
    *,
    mode: str = DEFAULT_IDENTITY_CHECK,
) -> tuple[bool, str | None]:
    """
    Compare the station id from the path against the certificate's CN.

    Returns:
        (ok, common_name). ok is False only when the mode is 'enforce'
        and the names disagree -- the caller then refuses the
        connection. In 'warn' the caller proceeds and the mismatch is
        logged and, by the caller, written to the event log.

    A station with no certificate returns (True, None) in 'warn' and
    (False, None) in 'enforce': under Profile 3 an unauthenticated
    station is not a station this CSMS should be talking to, but saying
    so on Day 7 would fail every connection made before the PKI is
    wired.
    """
    if mode == "off":
        return True, None

    common_name = peer_common_name(peer_certificate(connection))

    if common_name == station_id:
        return True, common_name

    if common_name is None:
        LOGGER.warning(
            "IDENTITY UNREADABLE: could not read a certificate identity for "
            "station %s while TLS is on. The identity check is therefore "
            "doing nothing for this connection -- run with --verbose for the "
            "transport detail. Treat this as a failure of the check, not as "
            "the station being fine.",
            station_id,
        )
    else:
        LOGGER.warning(
            "IDENTITY MISMATCH: station connected as %r but its certificate "
            "says %r -- one certificate holder claiming another station's "
            "identity is exactly the impersonation E5 demonstrates",
            station_id, common_name,
        )

    return mode != "enforce", common_name
