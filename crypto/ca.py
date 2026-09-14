"""
Certificate authority.

Issues X.509 certificates for charging stations and for the CSMS server,
signed by a single root key. Certificates and private keys are returned as
DER bytes, per Contract 1; PEM conversion happens only in crypto/store.py.

SERVER vs STATION certificates:
  - Station certificates are presented by the client (the station) and are
    NOT hostname-verified, so they carry no Subject Alternative Name.
  - The SERVER certificate IS hostname-verified by the station
    (check_hostname=True), so it MUST carry a SAN covering every name a
    station uses to reach the CSMS. issue_server_certificate() handles this;
    see docs/limitations.md R4 and Track A plan section 10.

DESIGN NOTE — why X.509 signing does not call CryptoProvider.sign() directly:
cryptography's CertificateBuilder.sign() requires a native key object and
refuses a duck-typed one (verified: raises TypeError). So the DER key bytes
from CryptoProvider.generate_keypair() are reloaded via load_der_private_key()
into a genuine EC key for the signing step. This is exact for classical mode.
It does NOT extend to Day 8's post-quantum provider -- this cryptography
version has no ML-DSA key type to reload into -- so __init__ raises for any
non-classical mode, deliberately, until crypto/pq.py and the Day 8 signing
path (see _signing_key docstring and docs/limitations.md) resolve it.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import NameOID

from crypto.provider import CryptoProvider

DEFAULT_VALIDITY_DAYS = 365
ROOT_VALIDITY_DAYS = 3650

DEFAULT_SERVER_SAN_NAMES = ["localhost"]
"""SAN names on the CSMS server certificate by default. Sufficient for local
development. For the Raspberry Pi bench node, pass the CSMS host's mDNS name
too, e.g. issue_server_certificate(san_names=["localhost", "host.local"]).
An mDNS .local name is preferred over an IP: DHCP reassigns IPs, which would
force reissuing the certificate on every network change."""


@dataclass
class IssuedCertificate:
    certificate_der: bytes
    private_key_der: bytes
    serial: str
    not_valid_after: datetime.datetime


class CertificateAuthority:
    def __init__(self, provider: CryptoProvider, common_name: str = "PQCharge Root CA") -> None:
        if provider.mode != "classical":
            raise NotImplementedError(
                f"CertificateAuthority signing is only implemented for classical "
                f"mode (Days 3-6); got mode={provider.mode!r}. See the module "
                f"docstring and docs/limitations.md for the Day 8 PQC plan."
            )
        self._provider = provider
        self._root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
        self._root_private_der, self._root_public_der = provider.generate_keypair()
        self._root_certificate = self._self_sign_root()

    @property
    def mode(self) -> str:
        return self._provider.mode

    @property
    def root_certificate_der(self) -> bytes:
        return self._root_certificate.public_bytes(serialization.Encoding.DER)

    def issue_station_certificate_with_new_key(
        self,
        station_id: str,
        valid_days: int = DEFAULT_VALIDITY_DAYS,
        san_names: list[str] | None = None,
    ) -> IssuedCertificate:
        """Generate a fresh station keypair and issue its certificate. Station
        certificates carry no SAN by default (client certs are not
        hostname-verified); san_names is accepted only for completeness."""
        private_der, public_der = self._provider.generate_keypair()
        return self._issue(station_id, public_der, valid_days, private_der, san_names)

    def issue_station_certificate(
        self,
        station_id: str,
        station_public_key_der: bytes,
        valid_days: int = DEFAULT_VALIDITY_DAYS,
        san_names: list[str] | None = None,
    ) -> IssuedCertificate:
        """Issue a certificate for a station's already-generated public key."""
        return self._issue(station_id, station_public_key_der, valid_days, None, san_names)

    def issue_server_certificate(
        self,
        common_name: str = "localhost",
        valid_days: int = DEFAULT_VALIDITY_DAYS,
        san_names: list[str] | None = None,
    ) -> IssuedCertificate:
        """
        Issue the CSMS server certificate, carrying a Subject Alternative Name.

        The station verifies this certificate's hostname against the address it
        connected to (check_hostname=True), so the SAN must include every such
        name. Defaults to ["localhost"]; for the Raspberry Pi bench node pass
        the CSMS host's mDNS name as well:

            ca.issue_server_certificate(san_names=["localhost", "host.local"])

        Verified: a client connecting to an IP but verifying against a .local
        SAN name succeeds, which is exactly the mDNS-resolved Pi scenario.
        """
        names = san_names if san_names is not None else list(DEFAULT_SERVER_SAN_NAMES)
        private_der, public_der = self._provider.generate_keypair()
        return self._issue(common_name, public_der, valid_days, private_der, names)

    def revoke(self, serial: str) -> None:
        raise NotImplementedError("revocation lands with certificate rotation, Day 5-6")

    # -- internal -------------------------------------------------

    def _issue(
        self,
        subject_name: str,
        public_key_der: bytes,
        valid_days: int,
        private_key_der: bytes | None,
        san_names: list[str] | None,
    ) -> IssuedCertificate:
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject_name)])
        now = datetime.datetime.now(datetime.timezone.utc)
        not_after = now + datetime.timedelta(days=valid_days)
        serial = x509.random_serial_number()
        public_key_obj = serialization.load_der_public_key(public_key_der)

        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(self._root_name)
            .public_key(public_key_obj)
            .serial_number(serial)
            .not_valid_before(now)
            .not_valid_after(not_after)
        )
        if san_names:
            builder = builder.add_extension(
                x509.SubjectAlternativeName([x509.DNSName(n) for n in san_names]),
                critical=False,
            )

        certificate = builder.sign(self._signing_key(), hashes.SHA256())

        return IssuedCertificate(
            certificate_der=certificate.public_bytes(serialization.Encoding.DER),
            private_key_der=private_key_der or b"",
            serial=format(serial, "x"),
            not_valid_after=not_after,
        )

    def _self_sign_root(self) -> x509.Certificate:
        now = datetime.datetime.now(datetime.timezone.utc)
        public_key_obj = serialization.load_der_public_key(self._root_public_der)
        builder = (
            x509.CertificateBuilder()
            .subject_name(self._root_name)
            .issuer_name(self._root_name)
            .public_key(public_key_obj)
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=ROOT_VALIDITY_DAYS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        )
        return builder.sign(self._signing_key(), hashes.SHA256())

    def _signing_key(self):
        """
        Root private key as a native cryptography object for
        CertificateBuilder.sign().

        DAY 8: breaks for the PQC provider -- self._root_private_der would hold
        ML-DSA bytes and load_der_private_key() has no ML-DSA type to reload
        them into (verified: no ml_dsa module in this cryptography version).
        Two options, in docs/limitations.md: (a) hand-roll the certificate ASN.1
        with asn1crypto and sign the tbsCertificate via CryptoProvider.sign()
        directly, or (b) use the application-layer transport fallback for the
        PQC path. __init__ guards against reaching this in non-classical mode.
        """
        return serialization.load_der_private_key(self._root_private_der, password=None)