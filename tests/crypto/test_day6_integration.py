"""
Day 3-6 integration test: the whole classical PKI path, end to end,
including a real mutual-TLS handshake -- not just unit-level checks of
each module in isolation. This is what actually gets exercised before
Track A's Day 7 TLS wiring.
"""

import shutil
import socket
import ssl
import threading
import time
from pathlib import Path

import pytest

from crypto.ca import CertificateAuthority
from crypto.classical import ClassicalProvider
from crypto.store import certificate_der_to_pem, save_certificate, save_private_key

TEST_DIR = Path("tests/_scratch_pki")
TEST_PORT = 8944


@pytest.fixture(autouse=True)
def clean_scratch_dir():
    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR)
    yield
    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR)


def _bootstrap(ca: CertificateAuthority) -> None:
    TEST_DIR.mkdir(parents=True, exist_ok=True)
    (TEST_DIR / "root.pem").write_bytes(certificate_der_to_pem(ca.root_certificate_der))

    server_issued = ca.issue_station_certificate_with_new_key("localhost")
    save_certificate(server_issued.certificate_der, "localhost", directory=TEST_DIR)
    save_private_key(server_issued.private_key_der, "localhost", directory=TEST_DIR)

    station_issued = ca.issue_station_certificate_with_new_key("CP001")
    save_certificate(station_issued.certificate_der, "CP001", directory=TEST_DIR)
    save_private_key(station_issued.private_key_der, "CP001", directory=TEST_DIR)


def _run_server(result: dict) -> None:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(
        certfile=TEST_DIR / "localhost.crt.pem", keyfile=TEST_DIR / "localhost.key.pem"
    )
    ctx.load_verify_locations(cafile=TEST_DIR / "root.pem")
    ctx.verify_mode = ssl.CERT_REQUIRED

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("localhost", TEST_PORT))
    sock.listen(1)
    try:
        conn, _ = sock.accept()
        tls = ctx.wrap_socket(conn, server_side=True)
        result["client_authenticated"] = tls.getpeercert() is not None
        # Explicit handoff -- see bootstrap_pki.py for why this matters
        # on Windows specifically.
        tls.sendall(b"S")
        ack = tls.recv(1)
        result["got_client_ack"] = ack == b"C"
        tls.close()
    except Exception as e:  # noqa: BLE001
        result["server_error"] = f"{type(e).__name__}: {e}"
    finally:
        sock.close()


def test_full_path_supports_real_mutual_tls_handshake():
    """
    CA issues a server cert and a station cert; both are saved to disk
    via crypto/store.py; a real TLS server and a real TLS client, each
    loading only the files just written, complete a mutual handshake
    with an explicit, timing-independent handoff before either side
    closes.

    This is the test that stands in for Day 7 before Day 7 happens.
    """
    ca = CertificateAuthority(ClassicalProvider())
    _bootstrap(ca)

    result: dict = {}
    server_thread = threading.Thread(target=_run_server, args=(result,))
    server_thread.start()
    time.sleep(0.3)

    client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_ctx.load_cert_chain(
        certfile=TEST_DIR / "CP001.crt.pem", keyfile=TEST_DIR / "CP001.key.pem"
    )
    client_ctx.load_verify_locations(cafile=TEST_DIR / "root.pem")

    with socket.create_connection(("localhost", TEST_PORT), timeout=2) as sock:
        with client_ctx.wrap_socket(sock, server_hostname="localhost") as tls:
            server_cert = tls.getpeercert()
            assert server_cert is not None

            sig = tls.recv(1)
            assert sig == b"S"
            tls.sendall(b"C")

    server_thread.join(timeout=2)
    assert "server_error" not in result, result.get("server_error")
    assert result.get("client_authenticated") is True
    assert result.get("got_client_ack") is True