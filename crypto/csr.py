"""
Station-side certificate requests — the charger half of live rotation.

Track B (crypto). Phase 5 of the Python 3.14 plan.

In OCPP 2.0.1's certificate flow the CHARGER makes its own key pair and sends
only a PKCS#10 certificate signing request (SignCertificate); the CSMS signs it
and returns the certificate (CertificateSigned). The private key never leaves
the charger -- which is what fixes findings F2/F13 (today the CSMS generates
every station key and sends it over classical TLS).

These helpers live in crypto/ because Contract 1's rule is that no module
outside crypto/ names an algorithm. The agent asks for "a post-quantum key and
request for station X" and gets PEM bytes back.
"""

from __future__ import annotations

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import NameOID

ROTATION_ALGORITHM = "ML-DSA-44"


def new_station_key_and_csr(station_id: str) -> tuple[bytes, str]:
    """
    Generate an ML-DSA-44 key pair ON THE STATION and a CSR for it.

    Returns (private_key_pem, csr_pem). The CSR's Common Name is the station
    id, because the CSMS's identity check compares the certificate CN with the
    station id in the connection URL (Track A finding F1), and the CA refuses a
    CSR whose CN is not the requesting station.
    """
    from cryptography.hazmat.primitives.asymmetric import mldsa

    key = mldsa.MLDSA44PrivateKey.generate()
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, station_id)]))
        .sign(key, None)  # pure ML-DSA: no separate hash
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return key_pem, csr.public_bytes(serialization.Encoding.PEM).decode("ascii")


def leaf_certificate_pem(certificate_chain_pem: str) -> bytes:
    """The first (leaf) certificate of a PEM chain, as PEM bytes. Raises
    ValueError if the chain holds no certificate."""
    certificates = x509.load_pem_x509_certificates(certificate_chain_pem.encode("ascii"))
    if not certificates:
        raise ValueError("certificate chain is empty")
    return certificates[0].public_bytes(serialization.Encoding.PEM)


def certificate_matches_key(certificate_pem: bytes, private_key_pem: bytes) -> bool:
    """Whether a certificate carries the public half of this private key --
    a charger must refuse a certificate issued for someone else's key."""
    try:
        certificate = x509.load_pem_x509_certificate(certificate_pem)
        key = serialization.load_pem_private_key(private_key_pem, password=None)
    except Exception:  # noqa: BLE001 - anything unreadable is "no"
        return False
    spki = serialization.PublicFormat.SubjectPublicKeyInfo
    return (
        certificate.public_key().public_bytes(serialization.Encoding.DER, spki)
        == key.public_key().public_bytes(serialization.Encoding.DER, spki)
    )


def certificate_common_name(certificate_pem: bytes) -> str | None:
    try:
        cns = x509.load_pem_x509_certificate(certificate_pem).subject.get_attributes_for_oid(
            NameOID.COMMON_NAME
        )
    except Exception:  # noqa: BLE001
        return None
    return cns[0].value if cns else None


def certificate_serial_hex(certificate_der: bytes | None) -> str | None:
    """Serial in the same lower-case hex form crypto/ca.py's IssuedCertificate
    uses, so serials from the CA and from a live connection compare equal."""
    if not certificate_der:
        return None
    try:
        return format(x509.load_der_x509_certificate(certificate_der).serial_number, "x")
    except Exception:  # noqa: BLE001
        return None
