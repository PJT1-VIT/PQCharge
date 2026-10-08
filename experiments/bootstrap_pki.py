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
    python -m experiments.bootstrap_pki --algorithm pqc      # ML-DSA-44 set in certs/pqc/

ALGORITHMS:
    classical (default)  ECDSA P-256, written to certs/        -- unchanged layout
    pqc                  ML-DSA-44,   written to certs/pqc/    -- same file names
  Server and agents pick a set with their existing --cert-dir flag
  (e.g. --cert-dir certs/pqc). The pqc set needs cryptography >= 50 and, for the
  handshake proof, Python 3.14 (OpenSSL >= 3.5). Each set has its OWN root.

Every certificate carries SKI/AKI (and the root KeyUsage), so the handshake proof
below runs with VERIFY_X509_STRICT -- the Python 3.13+ default (T6).

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
from crypto.tls_mode import PQ_SERVER_NAME
from crypto.store import (
    certificate_der_to_pem,
    save_certificate,
    save_private_key,
    measure_certificate_sizes,
)

CERT_DIR = PROJECT_ROOT / "certs"
CERT_DIRS = {"classical": CERT_DIR, "pqc": CERT_DIR / "pqc"}
ALGORITHM_LABEL = {"classical": "ECDSA P-256", "pqc": "ML-DSA-44"}
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


def _provider(algorithm: str):
    if algorithm == "pqc":
        from crypto.pq import PQProvider

        return PQProvider()
    return ClassicalProvider()


def build_pki(ids: list[str], algorithm: str = "classical", cert_dir: Path | None = None) -> CertificateAuthority:
    """Create the CA and write the root, server, and the given station
    certificates to disk (certs/ for classical, certs/pqc/ for pqc)."""
    cert_dir = cert_dir or CERT_DIRS[algorithm]
    print(f"Building PKI ({ALGORITHM_LABEL[algorithm]})...")
    ca = CertificateAuthority(_provider(algorithm))

    cert_dir.mkdir(parents=True, exist_ok=True)
    (cert_dir / "root.pem").write_bytes(
        certificate_der_to_pem(ca.root_certificate_der)
    )
    print(f"  root CA written: {cert_dir / 'root.pem'}")
    # The root's private key, so the running CSMS can sign the CSRs that
    # arrive during live certificate rotation with this same root
    # (CertificateAuthority.from_files). certs/ is gitignored.
    save_private_key(ca.root_private_key_der, "root", directory=cert_dir)
    print(f"  root CA key written: {cert_dir / 'root.key.pem'} (keep private)")

    # The ML-DSA server identity is selected by SNI (crypto/tls_mode.PQ_SERVER_NAME),
    # so its certificate must carry that name too.
    server_san = ["localhost", PQ_SERVER_NAME] if algorithm == "pqc" else None
    server_issued = ca.issue_server_certificate(SERVER_COMMON_NAME, san_names=server_san)
    save_certificate(server_issued.certificate_der, SERVER_COMMON_NAME, directory=cert_dir)
    save_private_key(server_issued.private_key_der, SERVER_COMMON_NAME, directory=cert_dir)
    print(f"  server identity written: {cert_dir / (SERVER_COMMON_NAME + '.crt.pem')}")

    for station_id in ids:
        issued = ca.issue_station_certificate_with_new_key(station_id)
        save_certificate(issued.certificate_der, station_id, directory=cert_dir)
        save_private_key(issued.private_key_der, station_id, directory=cert_dir)
        sizes = measure_certificate_sizes(issued.certificate_der, label=station_id)
        print(
            f"  {station_id} certificate written "
            f"(DER {sizes.der_bytes}B, PEM {sizes.pem_bytes}B, "
            f"serial {issued.serial})"
        )

    write_roots_bundle()
    return ca


def write_roots_bundle() -> Path | None:
    """certs/roots_all.pem = the classical root + the ML-DSA root, whenever
    both sets exist. A charger that is rotated from ECDSA to ML-DSA trusts this
    bundle, so it can verify the CSMS before AND after its switch."""
    parts = [CERT_DIRS["classical"] / "root.pem", CERT_DIRS["pqc"] / "root.pem"]
    if not all(p.is_file() for p in parts):
        return None
    bundle = CERT_DIR / "roots_all.pem"
    bundle.write_bytes(b"".join(p.read_bytes() for p in parts))
    print(f"  trust bundle written: {bundle} (classical + ML-DSA roots, for rotating chargers)")
    return bundle


def _run_test_server(result: dict, cert_dir: Path) -> None:
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
        certfile=cert_dir / f"{SERVER_COMMON_NAME}.crt.pem",
        keyfile=cert_dir / f"{SERVER_COMMON_NAME}.key.pem",
    )
    ctx.load_verify_locations(cafile=cert_dir / "root.pem")
    ctx.verify_mode = ssl.CERT_REQUIRED  # mutual TLS: require the client's cert
    ctx.verify_flags |= ssl.VERIFY_X509_STRICT  # T6: what Python 3.13+ defaults to

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


def verify_handshake(station_id: str = "CP0001", cert_dir: Path = CERT_DIR,
                     server_name: str = SERVER_COMMON_NAME) -> bool:
    """
    Prove the PKI material just written actually supports a real
    mutual-TLS handshake, station-to-server, before Track A wires
    anything into the real CSMS.

    Returns True if both sides authenticated each other and completed
    the explicit handoff cleanly.
    """
    print(f"\nAttempting a real mutual-TLS handshake ({station_id} -> server)...")
    result: dict = {}
    server_thread = threading.Thread(target=_run_test_server, args=(result, cert_dir))
    server_thread.start()
    time.sleep(0.3)  # let the listener bind before the client connects

    client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_ctx.load_cert_chain(
        certfile=cert_dir / f"{station_id}.crt.pem",
        keyfile=cert_dir / f"{station_id}.key.pem",
    )
    client_ctx.load_verify_locations(cafile=cert_dir / "root.pem")
    client_ctx.verify_flags |= ssl.VERIFY_X509_STRICT  # T6: what Python 3.13+ defaults to
    # check_hostname defaults to True here -- matches how a real
    # station verifies the CSMS's identity against its wss:// URL.

    handshake_ok = False
    try:
        with socket.create_connection(("localhost", HANDSHAKE_TEST_PORT), timeout=2) as sock:
            with client_ctx.wrap_socket(sock, server_hostname=server_name) as tls:
                server_cert = tls.getpeercert()
                server_cn = dict(x[0] for x in server_cert["subject"]).get("commonName")
                print(f"  client accepted server certificate (CN={server_cn}) "
                      f"| {tls.version()} {tls.cipher()[0]} | strict verification ON")

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
    parser.add_argument("--algorithm", choices=sorted(CERT_DIRS), default="classical",
                        help="certificate algorithm: classical (ECDSA, certs/) or pqc (ML-DSA-44, certs/pqc/)")
    args = parser.parse_args(argv)
    ids = station_ids(args.count, args.also)
    cert_dir = CERT_DIRS[args.algorithm]
    rel = cert_dir.relative_to(PROJECT_ROOT).as_posix()

    print("=" * 60)
    print("PQCharge — Day 7 PKI bootstrap")
    print("=" * 60)
    print(f"Project root resolved as: {PROJECT_ROOT}")

    build_pki(ids, args.algorithm, cert_dir)
    success = verify_handshake(
        ids[0], cert_dir, PQ_SERVER_NAME if args.algorithm == "pqc" else SERVER_COMMON_NAME
    )

    print("\n" + "=" * 60)
    if success:
        print("SUCCESS — mutual TLS handshake completed with real certificates.")
        print("\nFor Track A's server (ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)):")
        print(f"  certfile = {rel}/{SERVER_COMMON_NAME}.crt.pem")
        print(f"  keyfile  = {rel}/{SERVER_COMMON_NAME}.key.pem")
        print(f"  cafile (for verify_locations) = {rel}/root.pem")
        print(f"  verify_mode = ssl.CERT_REQUIRED")
        shown = ids if len(ids) <= 6 else ids[:3] + ["..."] + ids[-2:]
        print(f"\nStation identities available ({len(ids)}): {', '.join(shown)}")
        print(f"Each has {rel}/<id>.crt.pem and {rel}/<id>.key.pem "
              f"({ALGORITHM_LABEL[args.algorithm]})")
        if args.algorithm != "classical":
            print(f"Use them with --cert-dir {rel} on the server and the agents.")
    else:
        print("FAILED — see errors above. Do not proceed to Day 7 integration")
        print("until this passes on this machine.")
        sys.exit(1)


if __name__ == "__main__":
    main()