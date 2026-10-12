"""
E4 — would post-quantum certificates fit OCPP 2.0.1? (Track B, B-F5)

    python -m experiments.e4_cert_sizes

Builds real ML-DSA-44 / -65 / -87 X.509 certificates (crypto/pq_x509.py),
measures them in DER and PEM, checks each against OCPP 2.0.1's limits
(5,500 characters per certificate, 10,000 per chain), prints a table and
writes evidence/e4/e4_pq_certificates.json for the results page.

Track C's analysis (analysis/measures/e4_sizes.py) already measures every
artifact the running system produces (classical certificates, ML-DSA keys
and signatures, the DataTransfer messages). This adds the one thing the
running system does not have: post-quantum CERTIFICATES.
"""

from __future__ import annotations

import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

from crypto.pq_x509 import (CERT_LIMIT, CHAIN_LIMIT, issue_chain, real_certificates_available,
                             size_rows, verify_chain)

OUT = Path("evidence") / "e4" / "e4_sizes.json"


def main() -> int:
    import cryptography
    from cryptography.hazmat.backends.openssl.backend import backend

    real = real_certificates_available()
    if real:
        assert verify_chain(issue_chain("ML-DSA-44"))   # the certificates are real and valid
    else:
        print("NOTE: this `cryptography` cannot make ML-DSA certificates; sizes are "
              "COMPUTED from FIPS 204 (L43). pip install -U cryptography for real ones.\n")
    rows = size_rows()

    print(f"{'algorithm':10} {'artifact':26} {'DER':>6} {'PEM':>6} {'limit':>6}  verdict")
    for r in rows:
        verdict = "fits" if r["within_limit"] else "EXCEEDS"
        print(f"{r['algorithm']:10} {r['label']:26} {r['bytes']:>6} {r['pem_bytes']:>6} "
              f"{r['limit']:>6}  {verdict} ({r['headroom']:+d})")

    findings = []
    for level in ("ML-DSA-44", "ML-DSA-65", "ML-DSA-87"):
        mine = [r for r in rows if r["algorithm"].startswith(level)]
        cert = next(r for r in mine if r["label"] == "Station certificate")
        chain = next(r for r in mine if r["label"].startswith("Chain"))
        findings.append(
            f"{level}: station certificate {cert['pem_bytes']} chars "
            f"({'fits' if cert['within_limit'] else 'exceeds'} {CERT_LIMIT}); "
            f"station + root chain {chain['pem_bytes']} chars "
            f"({'fits' if chain['within_limit'] else 'exceeds'} {CHAIN_LIMIT})")
    print("\n" + "\n".join(findings))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "experiment": "E4",
        "what": "ML-DSA X.509 certificates vs OCPP 2.0.1 size limits",
        "method": "real certificates" if real else "computed (FIPS 204 sizes + measured X.509 overhead)",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_with": {"python": platform.python_version(),
                           "cryptography": cryptography.__version__,
                           "openssl": backend.openssl_version_text()},
        "limits": {"certificate": CERT_LIMIT, "chain": CHAIN_LIMIT},
        "rows": rows,
        "findings": findings,
    }, indent=2), encoding="utf-8")
    print(f"\nwritten: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
