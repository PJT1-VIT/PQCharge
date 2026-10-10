"""
Day 7 handoff — produces working PKI material and proves it end to end.

Builds a root CA, a server certificate (for the CSMS itself), and a
handful of station certificates using this project's real crypto/
modules, writes them to disk in the layout ssl.SSLContext expects, and
then runs an actual mutual-TLS handshake against them -- not a
simulation of one. If this script prints SUCCESS, Track A can load
these exact files into their server on Day 7 with no further work from
Track B.

Run from anywhere (path resolution is relative to this file, not the
current working directory):
    python -m experiments.bootstrap_pki                      # CP0001-CP0003
    python -m experiments.bootstrap_pki --count 50           # CP0001-CP0050
    python -m experiments.bootstrap_pki --also E6-SAP-01     # + a third-party charger
    python -m experiments.bootstrap_pki --count 50 --also CP0000 --server-san 192.168.43.10   # + the Pi (CP0000), server cert valid for the laptop's hotspot IP

Station ids are 4-digit (CP0001), agreed by all three tracks on Day 8. The CN
of each certificate IS the station id, because Track A's identity check
compares the CN with the id in the connection URL (finding F1).

Every run makes a NEW root CA, so all certificates are regenerated together:
a certificate from an earlier run will not verify against the new root.

Output: certs/ (at the project root) populated with root.pem,
localhost.crt.pem / localhost.key.pem (server identity), and one
.crt.pem / .key.pem pair per demo station. certs/ is gitignored --
these are regenerated, not committed.
"""

from __future__ import annotations

import argparse
import socket
import ssl
import sys
import threading
import time
from pathlib import Path

# Project root is the parent of this file's directory (experiments/),
# resolved absolutely so this script behaves the same whether launched
# as `python experiments/bootstrap_pki.py` from the root or
# `python bootstrap_pki.py` from inside experiments/ itself.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from crypto.ca import CertificateAuthority
from crypto.classical import ClassicalProvider
from crypto.store import (
    certificate_der_to_pem,
    save_certificate,
    save_private_key,
    measure_certificate_sizes,
)

CERT_DIR = PROJECT_ROOT / "certs"
SERVER_COMMON_NAME = "localhost"
DEFAULT_STATION_COUNT = 3


def station_ids(count: int, also: list[str] | None = None) -> list[str]:
    """CP0001..CP<count>, 4-digit, then any extra ids in the order given."""
    if not 1 <= count <= 9999:
        raise ValueError(f"--count must be 1..9999, got {count}")
    ids = [f"CP{n:04d}" for n in range(1, count + 1)]
    for extra in also or []:
        if extra not in ids:
            ids.append(extra)
    return ids
HANDSHAKE_TEST_PORT = 8943


def build_pki(ids: list[str], server_san: list[str] | None = None) -> CertificateAuthority:
    """Create the CA and write the root, server, and the given station
    certificates to disk under the project root's certs/ directory."""
    print("Building PKI...")
    ca = CertificateAuthority(ClassicalProvider())

    CERT_DIR.mkdir(parents=True, exist_ok=True)
    (CERT_DIR / "root.pem").write_bytes(
        certificate_der_to_pem(ca.root_certificate_der)
    )
    print(f"  root CA written: {CERT_DIR / 'root.pem'}")

    # B-F3: the server certificate must name every address a station dials:
    # "localhost" for the laptop's own chargers, plus the laptop's hotspot IP
    # (or .local name) for the Raspberry Pi (--server-san).
    san = [SERVER_COMMON_NAME] + [n for n in (server_san or []) if n != SERVER_COMMON_NAME]
    server_issued = ca.issue_server_certificate(SERVER_COMMON_NAME, san_names=san)
    save_certificate(server_issued.certificate_der, SERVER_COMMON_NAME, directory=CERT_DIR)
    save_private_key(server_issued.private_key_der, SERVER_COMMON_NAME, directory=CERT_DIR)
    print(f"  server identity written: {CERT_DIR / (SERVER_COMMON_NAME + '.crt.pem')} "
          f"(SAN: {', '.join(san)})")

    for station_id in ids:
        issued = ca.issue_station_certificate_with_new_key(station_id)
        save_certificate(issued.certificate_der, station_id, directory=CERT_DIR)
        save_private_key(issued.private_key_der, station_id, directory=CERT_DIR)
        sizes = measure_certificate_sizes(issued.certificate_der, label=station_id)
        print(
            f"  {station_id} certificate written "
            f"(DER {sizes.der_bytes}B, PEM {sizes.pem_bytes}B, "
            f"serial {issued.serial})"
        )

    return ca


def _run_test_server(result: dict) -> None:
    """
    Server side of the handshake proof.

    Reads the client's certificate, then explicitly signals "done
    reading" and waits for the client's acknowledgment before closing.
    Neither side infers the other is finished from timing -- this is
    what avoids the Windows-specific WinError 10053 that a bare
    close-immediately-after-handshake pattern can trigger.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(
        certfile=CERT_DIR / f"{SERVER_COMMON_NAME}.crt.pem",
        keyfile=CERT_DIR / f"{SERVER_COMMON_NAME}.key.pem",
    )
    ctx.load_verify_locations(cafile=CERT_DIR / "root.pem")
    ctx.verify_mode = ssl.CERT_REQUIRED  # mutual TLS: require the client's cert

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("localhost", HANDSHAKE_TEST_PORT))
    sock.listen(1)
    try:
        conn, _ = sock.accept()
        tls = ctx.wrap_socket(conn, server_side=True)
        peer_cert = tls.getpeercert()
        result["server_saw_client_cert"] = peer_cert is not None
        result["client_cn"] = dict(x[0] for x in peer_cert["subject"]).get("commonName")

        tls.sendall(b"S")          # "I have everything I need"
        ack = tls.recv(1)          # wait for the client's acknowledgment
        result["got_client_ack"] = ack == b"C"
        tls.close()
    except Exception as e:  # noqa: BLE001 -- surfaced to the printed summary below
        result["server_error"] = f"{type(e).__name__}: {e}"
    finally:
        sock.close()


def verify_handshake(station_id: str = "CP0001") -> bool:
    """
    Prove the PKI material just written actually supports a real
    mutual-TLS handshake, station-to-server, before Track A wires
    anything into the real CSMS.

    Returns True if both sides authenticated each other and completed
    the explicit handoff cleanly.
    """
    print(f"\nAttempting a real mutual-TLS handshake ({station_id} -> server)...")
    result: dict = {}
    server_thread = threading.Thread(target=_run_test_server, args=(result,))
    server_thread.start()
    time.sleep(0.3)  # let the listener bind before the client connects

    client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_ctx.load_cert_chain(
        certfile=CERT_DIR / f"{station_id}.crt.pem",
        keyfile=CERT_DIR / f"{station_id}.key.pem",
    )
    client_ctx.load_verify_locations(cafile=CERT_DIR / "root.pem")
    # check_hostname defaults to True here -- matches how a real
    # station verifies the CSMS's identity against its wss:// URL.

    handshake_ok = False
    try:
        with socket.create_connection(("localhost", HANDSHAKE_TEST_PORT), timeout=2) as sock:
            with client_ctx.wrap_socket(sock, server_hostname=SERVER_COMMON_NAME) as tls:
                server_cert = tls.getpeercert()
                server_cn = dict(x[0] for x in server_cert["subject"]).get("commonName")
                print(f"  client accepted server certificate (CN={server_cn})")

                sig = tls.recv(1)                # wait for server's "done reading" signal
                if sig == b"S":
                    tls.sendall(b"C")             # acknowledge
                    handshake_ok = True
    except ssl.SSLCertVerificationError as e:
        print(f"  FAILED — certificate verification error: {e}")
    except Exception as e:  # noqa: BLE001
        print(f"  FAILED — {type(e).__name__}: {e}")

    server_thread.join(timeout=2)

    if result.get("server_saw_client_cert"):
        print(f"  server accepted client certificate (CN={result.get('client_cn')})")
    if "server_error" in result:
        print(f"  server-side error: {result['server_error']}")
        handshake_ok = False
    if not result.get("got_client_ack"):
        handshake_ok = False

    return handshake_ok


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build the PKI and prove a mutual-TLS handshake.")
    parser.add_argument("--count", type=int, default=DEFAULT_STATION_COUNT,
                        help="issue CP0001..CP<count> (default 3)")
    parser.add_argument("--also", nargs="*", default=[], metavar="ID",
                        help="extra station ids, e.g. E6-SAP-01 for the third-party charger")
    parser.add_argument("--server-san", nargs="*", default=[], metavar="NAME",
                        help="extra names/IPs for the server certificate, e.g. the "
                             "laptop's hotspot IP for the Raspberry Pi: --server-san 192.168.43.10")
    args = parser.parse_args(argv)
    ids = station_ids(args.count, args.also)

    print("=" * 60)
    print("PQCharge — Day 7 PKI bootstrap")
    print("=" * 60)
    print(f"Project root resolved as: {PROJECT_ROOT}")

    build_pki(ids, args.server_san)
    success = verify_handshake(ids[0])

    print("\n" + "=" * 60)
    if success:
        print("SUCCESS — mutual TLS handshake completed with real certificates.")
        print("\nFor Track A's server (ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)):")
        print(f"  certfile = certs/{SERVER_COMMON_NAME}.crt.pem")
        print(f"  keyfile  = certs/{SERVER_COMMON_NAME}.key.pem")
        print(f"  cafile (for verify_locations) = certs/root.pem")
        print(f"  verify_mode = ssl.CERT_REQUIRED")
        shown = ids if len(ids) <= 6 else ids[:3] + ["..."] + ids[-2:]
        print(f"\nStation identities available ({len(ids)}): {', '.join(shown)}")
        print(f"Each has certs/<id>.crt.pem and certs/<id>.key.pem")
    else:
        print("FAILED — see errors above. Do not proceed to Day 7 integration")
        print("until this passes on this machine.")
        sys.exit(1)


if __name__ == "__main__":
    main()