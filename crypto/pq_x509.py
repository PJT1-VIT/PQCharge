"""
Post-quantum X.509 certificates (ML-DSA), for E4's size question only.

Track B, B-F5 (2026-10-10).

WHY THIS EXISTS
PQCharge does NOT use ML-DSA certificates on the wire: TLS stays classical
and the post-quantum identity is an application-layer key check (Option B,
Contract 7 "hybrid"). Pure-PQC mode, where certificates themselves would be
ML-DSA, is future work (L17, L29). E4 still has to answer the question the
design document asks: *would* post-quantum certificates fit OCPP 2.0.1's
limits (5,500 characters per certificate, 10,000 per chain)?

Recent `cryptography` releases (50.0.2 here, with OpenSSL 4.0.3 bundled) can
build and sign real ML-DSA X.509 certificates, so the answer is measured on
real certificates built exactly like our classical ones (same names, same
extensions: BasicConstraints, SKI/AKI, SAN on the server) -- not estimated.

Nothing in the running system imports this module.

L43 (decided 2026-10-12): `requirements.txt` keeps `cryptography>=42`. Where
the installed `cryptography` cannot make ML-DSA certificates, size_rows()
COMPUTES the sizes instead -- FIPS 204 key and signature sizes plus the
X.509 overhead measured on real certificates -- and labels every such row
"(computed)". So the E4 rows appear on every laptop, and the label says
which kind they are.
"""

from __future__ import annotations

import datetime
from typing import Any

CERT_LIMIT = 5500
CHAIN_LIMIT = 10000
"""OCPP 2.0.1 limits, in characters of PEM text (the form OCPP carries).
Same constants as analysis/measures/e4_sizes.py."""

LEVELS = ("ML-DSA-44", "ML-DSA-65", "ML-DSA-87")
"""FIPS 204 parameter sets. PQCharge's identity key is ML-DSA-44."""


FIPS204_SIZES = {
    "ML-DSA-44": (1312, 2420),
    "ML-DSA-65": (1952, 3309),
    "ML-DSA-87": (2592, 4627),
}
"""(public key bytes, signature bytes) per parameter set, FIPS 204 Table 2."""

X509_OVERHEAD = {"station": 250, "server": 275, "root": 230}
"""DER bytes of a certificate beyond its public key and signature, measured
on the real certificates built by issue_chain() (cryptography 50.0.2,
2026-10-10): names, validity, extensions, algorithm identifiers, framing.
Used only by the computed fallback (L43)."""


def available() -> bool:
    """Whether size_rows() can produce rows. Always True since L43: real
    certificates when possible, computed sizes otherwise."""
    return True


def real_certificates_available() -> bool:
    """Whether the installed `cryptography` can make ML-DSA certificates."""
    try:
        from cryptography.hazmat.primitives.asymmetric import mldsa  # noqa: F401
        return True
    except ImportError:
        return False


def _key_class(level: str):
    from cryptography.hazmat.primitives.asymmetric import mldsa

    return getattr(mldsa, "MLDSA" + level.split("-")[-1] + "PrivateKey")


def issue_chain(level: str = "ML-DSA-44") -> dict[str, bytes]:
    """
    A root CA, a station certificate and a server certificate, all ML-DSA at
    `level`, built like crypto/ca.py builds the classical ones. Returns DER.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.x509.oid import NameOID

    key_cls = _key_class(level)
    now = datetime.datetime.now(datetime.timezone.utc)
    root_key = key_cls.generate()
    root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PQCharge Root CA")])

    root = (
        x509.CertificateBuilder()
        .subject_name(root_name).issuer_name(root_name)
        .public_key(root_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(root_key.public_key()), critical=False)
        .sign(root_key, None)                      # ML-DSA signs with no separate hash
    )

    def leaf(common_name: str, san: list[str] | None) -> bytes:
        key = key_cls.generate()
        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
            .issuer_name(root_name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()),
                           critical=False)
        )
        if san:
            builder = builder.add_extension(
                x509.SubjectAlternativeName([x509.DNSName(n) for n in san]), critical=False)
        return builder.sign(root_key, None).public_bytes(serialization.Encoding.DER)

    return {
        "root": root.public_bytes(serialization.Encoding.DER),
        "station": leaf("CP0001", None),
        "server": leaf("localhost", ["localhost"]),
    }


def _pem_len(der: bytes) -> int:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    return len(x509.load_der_x509_certificate(der).public_bytes(serialization.Encoding.PEM))


def _row(label: str, algorithm: str, der: int, pem: int, limit: int) -> dict[str, Any]:
    """Same shape as analysis/measures/e4_sizes.py's rows."""
    return {
        "group": "post-quantum X.509 (not used on the wire; pure-PQC future work)",
        "label": label,
        "algorithm": algorithm,
        "bytes": der,
        "pem_bytes": pem,
        "limit": limit,
        "within_limit": pem <= limit,
        "headroom": limit - pem,
    }


def pem_length(der_len: int) -> int:
    """Characters of a PEM certificate for a DER body of der_len bytes:
    header line, base64 in 64-character lines, footer line."""
    b64 = 4 * ((der_len + 2) // 3)
    lines = (b64 + 63) // 64
    return len("-----BEGIN CERTIFICATE-----\n") + b64 + lines + len("-----END CERTIFICATE-----\n")


def computed_der(level: str, kind: str) -> int:
    """DER size of an ML-DSA certificate, computed (no cryptography needed)."""
    pk, sig = FIPS204_SIZES[level]
    return X509_OVERHEAD[kind] + pk + sig


def size_rows(levels: tuple[str, ...] = LEVELS) -> list[dict[str, Any]]:
    """E4 rows for ML-DSA certificates and the station + root chain: from
    real certificates when the installed `cryptography` can build them,
    otherwise computed and labelled "(computed)" (L43)."""
    if not real_certificates_available():
        return _computed_rows(levels)
    rows: list[dict[str, Any]] = []
    for level in levels:
        chain = issue_chain(level)
        for label, key, limit in (("Station certificate", "station", CERT_LIMIT),
                                  ("Server certificate", "server", CERT_LIMIT),
                                  ("Root CA certificate", "root", CERT_LIMIT)):
            rows.append(_row(label, level, len(chain[key]), _pem_len(chain[key]), limit))
        rows.append(_row("Chain (station + root)", level,
                         len(chain["station"]) + len(chain["root"]),
                         _pem_len(chain["station"]) + _pem_len(chain["root"]), CHAIN_LIMIT))
    return rows


def verify_chain(chain: dict[str, bytes]) -> bool:
    """True if the root's ML-DSA key verifies both leaf signatures (sanity)."""
    from cryptography import x509

    root = x509.load_der_x509_certificate(chain["root"])
    for key in ("station", "server"):
        leaf = x509.load_der_x509_certificate(chain[key])
        root.public_key().verify(leaf.signature, leaf.tbs_certificate_bytes)
    return True


def _computed_rows(levels: tuple[str, ...]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for level in levels:
        label_alg = f"{level} (computed)"
        sizes = {k: computed_der(level, k) for k in ("station", "server", "root")}
        for label, key, limit in (("Station certificate", "station", CERT_LIMIT),
                                  ("Server certificate", "server", CERT_LIMIT),
                                  ("Root CA certificate", "root", CERT_LIMIT)):
            rows.append(_row(label, label_alg, sizes[key], pem_length(sizes[key]), limit))
        rows.append(_row("Chain (station + root)", label_alg,
                         sizes["station"] + sizes["root"],
                         pem_length(sizes["station"]) + pem_length(sizes["root"]), CHAIN_LIMIT))
    return rows
