"""
A refused TLS handshake is now visible (2026-10-14, runbook v1.2 V3).

csms/transport.py observe_tls_refusals() + csms/server.py _tls_refused():
a charger refused DURING the TLS handshake -- certificate not signed by our
CA (forged, or from an old CA), or no certificate at all -- writes ONE
Contract 3 line:

    connection_failed, station_id None, outcome "rejected",
    payload {reason: "tls_rejected", tls_error, detail, tls_detail}

No station id: the handshake fails before the charger's id (in the URL) is
sent. A good certificate writes no such line.

Track A's files, changed by Track C with the user's OK; tests written by
Track C. Real TLS, real certificates (made in tmp_path), a real CSMS socket.
"""

from __future__ import annotations

import asyncio
import json
import socket
import ssl

import pytest
from websockets.asyncio.server import serve

from csms.server import CSMS, SUBPROTOCOLS
from csms.transport import TLS_REFUSAL_WORDS, build_server_context, observe_tls_refusals
from experiments import bootstrap_pki as bp
from experiments import forge_cert


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    d = tmp_path_factory.mktemp("pki")
    old = bp.CERT_DIR
    bp.CERT_DIR = d
    try:
        bp.build_pki(["CP0001"])
    finally:
        bp.CERT_DIR = old
    forged = d / "attacker"
    forge_cert.forge("CP0001", forged)
    return d, forged


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _server_ctx(d):
    return build_server_context(d / "localhost.crt.pem", d / "localhost.key.pem", d / "root.pem")


def _client_ctx(d, cert=None, key=None):
    c = ssl.create_default_context(cafile=str(d / "root.pem"))
    if cert is not None:
        c.load_cert_chain(certfile=str(cert), keyfile=str(key))
    return c


async def _try(port, ctx):
    """Open TLS and read once; True if the server let us in."""
    try:
        r, w = await asyncio.open_connection("127.0.0.1", port, ssl=ctx, server_hostname="localhost")
        w.write(b"GET /api/health HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await w.drain()
        data = await asyncio.wait_for(r.read(1), 5)
        w.close()
        return bool(data)
    except (ssl.SSLError, ConnectionError, OSError):
        return False


async def _csms_scenario(tmp_path, d, clients):
    port = _free_port()
    log = tmp_path / "e.jsonl"
    csms = CSMS(host="127.0.0.1", port=port, log_path=str(log), db_path=None,
                ws_ping_interval=None, migration_mode="off", ssl_context=_server_ctx(d))
    results = []
    async with serve(csms.on_connect, "127.0.0.1", port, subprotocols=SUBPROTOCOLS,
                     process_request=csms.process_request, ssl=csms.ssl_context):
        for ctx in clients:
            results.append(await _try(port, ctx))
        await asyncio.sleep(0.2)
    csms.log.close()
    lines = [json.loads(x) for x in log.read_text().splitlines()]
    return results, [e for e in lines if e["event_type"] == "connection_failed"]


def test_forged_and_missing_certificates_each_write_one_line(tmp_path, pki):
    d, forged = pki
    clients = [_client_ctx(d, forged / "CP0001.crt.pem", forged / "CP0001.key.pem"),   # forged CA
               _client_ctx(d)]                                                         # no certificate
    results, failed = asyncio.run(asyncio.wait_for(_csms_scenario(tmp_path, d, clients), 30))
    assert results == [False, False]
    assert [e["payload"]["tls_error"] for e in failed] == [
        "CERTIFICATE_VERIFY_FAILED", "PEER_DID_NOT_RETURN_A_CERTIFICATE"]
    for e in failed:
        assert e["station_id"] is None and e["outcome"] == "rejected"
        assert e["payload"]["reason"] == "tls_rejected"
        assert e["payload"]["detail"] == TLS_REFUSAL_WORDS[e["payload"]["tls_error"]]
        assert e["payload"]["tls_detail"]


def test_a_good_certificate_writes_no_refusal(tmp_path, pki):
    d, _ = pki
    clients = [_client_ctx(d, d / "CP0001.crt.pem", d / "CP0001.key.pem")]
    results, failed = asyncio.run(asyncio.wait_for(_csms_scenario(tmp_path, d, clients), 30))
    assert results == [True] and failed == []


def test_a_broken_callback_never_breaks_the_handshake_refusal(pki):
    """The observer must only watch: if reporting fails, TLS still refuses."""
    d, forged = pki

    def boom(*_):
        raise RuntimeError("reporting broke")

    async def scenario():
        ctx = _server_ctx(d)
        observe_tls_refusals(ctx, boom)
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0, ssl=ctx)
        port = server.sockets[0].getsockname()[1]
        try:
            bad = await _try(port, _client_ctx(d, forged / "CP0001.crt.pem", forged / "CP0001.key.pem"))
        finally:
            server.close()
            await server.wait_closed()
        return bad

    assert asyncio.run(asyncio.wait_for(scenario(), 30)) is False


def test_forged_certificate_names_the_charger_but_not_our_ca(pki):
    from cryptography import x509

    d, forged = pki
    cert = x509.load_pem_x509_certificate((forged / "CP0001.crt.pem").read_bytes())
    ours = x509.load_pem_x509_certificate((d / "root.pem").read_bytes())
    assert cert.subject.rfc4514_string() == "CN=CP0001"
    assert cert.issuer != ours.subject
