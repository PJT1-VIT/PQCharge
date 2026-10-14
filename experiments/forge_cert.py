"""
Make a FORGED charger certificate for the demo's attack scene (runbook v1.2, V3).

Track C, 2026-10-14. A demo helper, not an experiment: nothing it does is
measured.

--------------------------------------------------------------------
IN PLAIN WORDS

An attacker who does not have our CA can still make a certificate that
SAYS "CP0045": they just sign it with their own, home-made CA. This script
does exactly that, with the project's own crypto code (crypto/ca.py), and
writes it to certs/attacker/ (gitignored, like every *.pem):

    certs/attacker/CP0045.crt.pem      "CN=CP0045", signed by the attacker's CA
    certs/attacker/CP0045.key.pem      its private key
    certs/attacker/attacker_root.pem   the attacker's CA (for reference only)

Then run the normal charger with it (the runbook shows the command):

    python -m agent.station --station-id CP0045 ... --cert certs\\attacker\\CP0045.crt.pem
           --key certs\\attacker\\CP0045.key.pem --ca certs\\root.pem

The server refuses it DURING the TLS handshake: the certificate is not
signed by our CA. The server writes one `connection_failed` line with
reason "tls_rejected" (csms/server.py, added 2026-10-14) and the dashboard
shows an "unknown device" tile. No charger id is known at that point -- the
charger never got as far as saying who it is.

An OLD certificate (from an earlier bootstrap_pki run, i.e. an older CA) is
refused the same way and for the same reason.

--probe-no-cert: instead, connect with NO client certificate at all (the
other TLS refusal: "no client certificate presented").

Never use this against a server you do not run.
"""

from __future__ import annotations

import argparse
import socket
import ssl
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from crypto.ca import CertificateAuthority  # noqa: E402
from crypto.classical import ClassicalProvider  # noqa: E402
from crypto.store import certificate_der_to_pem, save_certificate, save_private_key  # noqa: E402

DEFAULT_OUT = PROJECT_ROOT / "certs" / "attacker"
ATTACKER_CA_NAME = "Totally Legit Charging CA"   # not our "PQCharge Root CA"


def forge(station_id: str, out_dir: Path) -> tuple[Path, Path]:
    """Write a certificate for station_id signed by a brand-new, untrusted CA."""
    out_dir.mkdir(parents=True, exist_ok=True)
    attacker_ca = CertificateAuthority(ClassicalProvider(), common_name=ATTACKER_CA_NAME)
    issued = attacker_ca.issue_station_certificate_with_new_key(station_id)
    cert = save_certificate(issued.certificate_der, station_id, directory=out_dir)
    key = save_private_key(issued.private_key_der, station_id, directory=out_dir)
    (out_dir / "attacker_root.pem").write_bytes(certificate_der_to_pem(attacker_ca.root_certificate_der))
    return cert, key


def probe_no_cert(host: str, port: int, ca: Path) -> str:
    """Open a TLS connection WITHOUT a client certificate; return what happened."""
    ctx = ssl.create_default_context(cafile=str(ca))
    try:
        with socket.create_connection((host, port), timeout=5) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                # TLS 1.3: the server checks the client certificate after the
                # client's Finished, so the refusal arrives on the first read.
                tls.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
                data = tls.recv(1)
                if not data:
                    return "refused by the server: it closed the connection during the TLS handshake"
                return "NOT refused?! got %r" % data
    except (ssl.SSLError, ConnectionError, OSError) as exc:
        return f"refused by the server: {type(exc).__name__}: {exc}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m experiments.forge_cert",
                                description="Forged charger certificate for the demo attack scene.")
    p.add_argument("--station-id", default="CP0045")
    p.add_argument("--out", default=str(DEFAULT_OUT), help="where to write it (default certs/attacker)")
    p.add_argument("--probe-no-cert", action="store_true",
                   help="instead: connect with NO client certificate and show the refusal")
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=9000)
    p.add_argument("--ca", default=str(PROJECT_ROOT / "certs" / "root.pem"),
                   help="our root CA, so the probe trusts the server")
    args = p.parse_args(argv)
    if args.probe_no_cert:
        print(probe_no_cert(args.host, args.port, Path(args.ca)))
        return 0
    cert, key = forge(args.station_id, Path(args.out))
    print(f"forged certificate for {args.station_id} (signed by {ATTACKER_CA_NAME!r}, NOT our CA):")
    print(f"  {cert}")
    print(f"  {key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
