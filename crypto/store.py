"""
Key and certificate storage — disk persistence, and DER/PEM conversion
at the OCPP boundary.

CryptoProvider and CertificateAuthority work exclusively in DER bytes,
per Contract 1. PEM only exists at the edges: when a certificate needs
to go out over OCPP's SignCertificate/CertificateSigned messages (which
carry PEM strings per the OCPP 2.0.1 specification), or when a human
needs to read a file. This module is that edge.

Also the source of the artifact-size table (a reduced E4): the whole
reason to distinguish DER and PEM sizes here is that PNNL-35760
documents PEM's base64 encoding inflating certificate size by over a
third relative to DER, which is what pushes some PQC certificates past
OCPP's 5,500-byte field limit. measure_sizes() below produces exactly
the pair of numbers that finding is stated in terms of.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization

DEFAULT_CERT_DIR = Path("certs")


@dataclass
class ArtifactSizes:
    """
    DER and PEM byte counts for one artifact (a certificate, typically).

    Read directly by the E4-lite table in experiments/ -- this is the
    dataclass that becomes one row of that table.
    """

    label: str
    der_bytes: int
    pem_bytes: int

    @property
    def inflation_ratio(self) -> float:
        """How much larger PEM is than DER, as a multiplier."""
        return self.pem_bytes / self.der_bytes if self.der_bytes else 0.0


# -- DER <-> PEM conversion -------------------------------------------
#
# These are pure re-encodings: the object is loaded once from the input
# format and re-emitted in the output format by the same library that
# parsed it, so no field is reinterpreted or altered in the process.


def certificate_der_to_pem(certificate_der: bytes) -> bytes:
    """Convert a DER-encoded certificate to PEM."""
    cert = x509.load_der_x509_certificate(certificate_der)
    return cert.public_bytes(serialization.Encoding.PEM)


def certificate_pem_to_der(certificate_pem: bytes) -> bytes:
    """Convert a PEM-encoded certificate to DER."""
    cert = x509.load_pem_x509_certificate(certificate_pem)
    return cert.public_bytes(serialization.Encoding.DER)


def private_key_der_to_pem(private_key_der: bytes) -> bytes:
    """
    Convert a DER-encoded (PKCS8) private key to PEM.

    Written unencrypted, matching how ClassicalProvider and the CA
    already hold keys -- this is a demo CA, not a production one; a
    real deployment would add passphrase encryption here.
    """
    key = serialization.load_der_private_key(private_key_der, password=None)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def private_key_pem_to_der(private_key_pem: bytes) -> bytes:
    """Convert a PEM-encoded private key to DER (PKCS8)."""
    key = serialization.load_pem_private_key(private_key_pem, password=None)
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


# -- disk persistence --------------------------------------------------


def save_certificate(
    certificate_der: bytes,
    station_id: str,
    directory: Path | str = DEFAULT_CERT_DIR,
) -> Path:
    """
    Write a certificate to disk as PEM, the human-readable and OCPP-
    transmittable form.

    Returns the path written to. Directory is created if absent; it is
    gitignored (see .gitignore's certs/ entry) and must stay that way.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{station_id}.crt.pem"
    path.write_bytes(certificate_der_to_pem(certificate_der))
    return path


def save_private_key(
    private_key_der: bytes,
    station_id: str,
    directory: Path | str = DEFAULT_CERT_DIR,
) -> Path:
    """
    Write a private key to disk as PEM.

    Same directory as save_certificate() by default. Never call this
    with a directory outside .gitignore's certs/ entry.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{station_id}.key.pem"
    path.write_bytes(private_key_der_to_pem(private_key_der))
    return path


def load_certificate(
    station_id: str,
    directory: Path | str = DEFAULT_CERT_DIR,
) -> bytes:
    """Read a certificate back from disk, returned as DER."""
    path = Path(directory) / f"{station_id}.crt.pem"
    return certificate_pem_to_der(path.read_bytes())


def load_private_key(
    station_id: str,
    directory: Path | str = DEFAULT_CERT_DIR,
) -> bytes:
    """Read a private key back from disk, returned as DER."""
    path = Path(directory) / f"{station_id}.key.pem"
    return private_key_pem_to_der(path.read_bytes())


# -- size measurement (E4-lite) -----------------------------------------


def measure_certificate_sizes(certificate_der: bytes, label: str = "certificate") -> ArtifactSizes:
    """
    DER and PEM sizes for one certificate.

    PEM is base64 (4 output bytes per 3 input bytes, ~33% larger before
    header/footer lines are even added), which is the concrete
    mechanism behind PNNL-35760's finding that a Dilithium2 certificate
    grows from 4149 bytes in DER to 5689 bytes in PEM. This function is
    how the same comparison is produced for whatever this project's own
    provider issues, classical today and post-quantum from Day 8.
    """
    pem = certificate_der_to_pem(certificate_der)
    return ArtifactSizes(label=label, der_bytes=len(certificate_der), pem_bytes=len(pem))


def measure_chain_sizes(certificate_ders: list[bytes], label: str = "chain") -> ArtifactSizes:
    """
    DER and PEM sizes for a certificate chain (concatenated PEM blocks,
    as OCPP's InstallCertificate / CertificateSigned messages carry a
    chain).

    Checked against OCPP's two separate limits in the E4-lite table:
    5,500 bytes for a single certificate, 10,000 bytes for a chain.
    """
    der_total = sum(len(c) for c in certificate_ders)
    pem_blocks = [certificate_der_to_pem(c) for c in certificate_ders]
    pem_total = sum(len(b) for b in pem_blocks)
    return ArtifactSizes(label=label, der_bytes=der_total, pem_bytes=pem_total)