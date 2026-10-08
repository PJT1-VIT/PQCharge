"""
Certificate authority.

Issues X.509 certificates for charging stations and for the CSMS server,
signed by a single root key. Certificates and private keys are returned as
DER bytes, per Contract 1; PEM conversion happens only in crypto/store.py.

TWO MODES (one CA instance serves exactly one):
  - classical: ECDSA P-256 root and leaves, signed with SHA-256.
  - pqc:       ML-DSA-44 root and leaves (FIPS 204; pure ML-DSA, no separate
               hash). Needs cryptography >= 50; TLS with these certificates
               needs OpenSSL >= 3.5 (Python 3.14's bundled OpenSSL).
  "hybrid" has no CA of its own: in the hybrid TLS mode the certificates are
  classical (ECDSA) and only the key exchange is hybrid, so a hybrid fleet uses
  a classical CA.

STRICT VERIFICATION (T6):
  Python 3.13+ ssl.create_default_context() turns on VERIFY_X509_STRICT, which
  rejects certificates without an Authority Key Identifier ("Missing Authority
  Key Identifier"). Every certificate now carries:
    - Subject Key Identifier            (root and leaves)
    - Authority Key Identifier          (leaves -> the root's key)
    - KeyUsage keyCertSign + cRLSign    (root, critical)
    - BasicConstraints CA:TRUE          (root, critical)

SERVER vs STATION certificates:
  - Station certificates are presented by the client (the station) and are
    NOT hostname-verified, so they carry no Subject Alternative Name.
  - The SERVER certificate IS hostname-verified by the station
    (check_hostname=True), so it MUST carry a SAN covering every name a
    station uses to reach the CSMS. issue_server_certificate() handles this;
    see docs/limitations.md R4 and Track A plan section 10.

KEY FORMATS ACROSS THE BOUNDARY:
  - Private keys returned in IssuedCertificate.private_key_der are always
    PKCS8 DER, in both modes, so crypto/store.py and ssl.load_cert_chain()
    treat them the same. (PQProvider itself hands out the 32-byte ML-DSA seed;
    this module wraps it into PKCS8.)
  - Public keys passed to issue_station_certificate() may be SubjectPublicKeyInfo
    DER (both modes) or, in pqc mode, the raw 1312-byte ML-DSA-44 public key
    that PQProvider.generate_keypair() returns.

DESIGN NOTE — why X.509 signing does not call CryptoProvider.sign() directly:
cryptography's CertificateBuilder.sign() requires a native key object and
refuses a duck-typed one. So the provider's key bytes are reloaded into a
native key object for the signing step (_signing_key).
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
SUPPORTED_MODES = ("classical", "pqc")

DEFAULT_SERVER_SAN_NAMES = ["localhost"]
"""SAN names on the CSMS server certificate by default. Sufficient for local
development. For the Raspberry Pi bench node, pass the CSMS host's mDNS name
too, e.g. issue_server_certificate(san_names=["localhost", "host.local"]).
An mDNS .local name is preferred over an IP: DHCP reassigns IPs, which would
force reissuing the certificate on every network change."""


class CSRRejected(ValueError):
    """A certificate signing request was refused (bad signature, wrong
    station id, or a key of the wrong algorithm for this CA)."""


@dataclass
class IssuedCertificate:
    certificate_der: bytes
    private_key_der: bytes
    serial: str
    not_valid_after: datetime.datetime


class CertificateAuthority:
    def __init__(self, provider: CryptoProvider, common_name: str = "PQCharge Root CA") -> None:
        if provider.mode not in SUPPORTED_MODES:
            raise NotImplementedError(
                f"CertificateAuthority supports modes {SUPPORTED_MODES}; got "
                f"mode={provider.mode!r}. A hybrid fleet uses classical (ECDSA) "
                f"certificates with hybrid key exchange, so it uses a classical CA."
            )
        self._provider = provider
        if provider.mode == "pqc":
            # Fails here, loudly, on cryptography < 50 (no ML-DSA).
            from cryptography.hazmat.primitives.asymmetric import mldsa

            self._mldsa = mldsa
        self._root_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
        self._root_private, self._root_public = provider.generate_keypair()
        self._root_certificate = self._self_sign_root()

    @classmethod
    def from_files(
        cls,
        provider: CryptoProvider,
        root_certificate_pem: bytes,
        root_private_key_pem: bytes,
    ) -> "CertificateAuthority":
        """
        Reload a CA that experiments/bootstrap_pki.py wrote to disk
        (root.pem + root.key.pem), so a running CSMS can sign the CSRs that
        arrive during live certificate rotation with the SAME root the
        chargers already trust. Raises ValueError if the key does not belong
        to the certificate or is not this provider's algorithm.
        """
        if provider.mode not in SUPPORTED_MODES:
            raise NotImplementedError(f"CertificateAuthority supports modes {SUPPORTED_MODES}")
        ca = cls.__new__(cls)
        ca._provider = provider
        if provider.mode == "pqc":
            from cryptography.hazmat.primitives.asymmetric import mldsa

            ca._mldsa = mldsa
        certificate = x509.load_pem_x509_certificate(root_certificate_pem)
        key = serialization.load_pem_private_key(root_private_key_pem, password=None)
        if not ca._key_matches_mode(key.public_key()):
            raise ValueError(f"root key type {type(key).__name__} does not match mode {provider.mode!r}")
        spki = serialization.PublicFormat.SubjectPublicKeyInfo
        if (key.public_key().public_bytes(serialization.Encoding.DER, spki)
                != certificate.public_key().public_bytes(serialization.Encoding.DER, spki)):
            raise ValueError("root private key does not belong to the root certificate")
        ca._root_certificate = certificate
        ca._root_name = certificate.subject
        ca._root_public = certificate.public_key().public_bytes(serialization.Encoding.DER, spki)
        ca._root_private = (
            key.private_bytes_raw() if provider.mode == "pqc"
            else key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption())
        )
        return ca

    @property
    def root_private_key_der(self) -> bytes:
        """The root's private key as PKCS8 DER -- for bootstrap_pki to save
        next to root.pem (gitignored certs/), so the CSMS can reload the CA."""
        return self._private_der(self._root_private)

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
        private, public = self._provider.generate_keypair()
        return self._issue(
            station_id, self._load_public(public), valid_days, self._private_der(private), san_names
        )

    def issue_station_certificate(
        self,
        station_id: str,
        station_public_key_der: bytes,
        valid_days: int = DEFAULT_VALIDITY_DAYS,
        san_names: list[str] | None = None,
    ) -> IssuedCertificate:
        """Issue a certificate for a station's already-generated public key
        (SubjectPublicKeyInfo DER, or the raw ML-DSA-44 public key in pqc mode)."""
        return self._issue(
            station_id, self._load_public(station_public_key_der), valid_days, None, san_names
        )

    def issue_certificate_from_csr(
        self,
        csr: bytes | str,
        expected_station_id: str | None = None,
        valid_days: int = DEFAULT_VALIDITY_DAYS,
    ) -> IssuedCertificate:
        """
        Issue a station certificate for a PKCS#10 CSR (PEM or DER) -- the OCPP
        SignCertificate flow. The station made the key pair itself and keeps the
        private key; only the public key and a proof of possession arrive here,
        so IssuedCertificate.private_key_der is empty.

        Refused (CSRRejected) when:
          - the CSR's self-signature does not verify (no proof of possession),
          - it has no Common Name, or the CN differs from expected_station_id,
          - its key is not this CA's algorithm (ML-DSA-44 for pqc, EC P-256 for
            classical) -- a pqc rotation must never be satisfied with a
            classical key.
        """
        raw = csr.encode() if isinstance(csr, str) else csr
        try:
            request = (
                x509.load_pem_x509_csr(raw)
                if raw.lstrip().startswith(b"-----BEGIN")
                else x509.load_der_x509_csr(raw)
            )
        except ValueError as exc:
            raise CSRRejected(f"CSR could not be parsed: {exc}") from exc
        if not request.is_signature_valid:
            raise CSRRejected("CSR signature is invalid (no proof of possession)")
        cns = request.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        if not cns:
            raise CSRRejected("CSR has no Common Name")
        cn = cns[0].value
        if expected_station_id is not None and cn != expected_station_id:
            raise CSRRejected(f"CSR Common Name {cn!r} != station {expected_station_id!r}")
        public_key = request.public_key()
        if not self._key_matches_mode(public_key):
            raise CSRRejected(
                f"CSR key type {type(public_key).__name__} does not match this "
                f"CA's mode {self.mode!r}"
            )
        return self._issue(cn, public_key, valid_days, None, None)

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
        private, public = self._provider.generate_keypair()
        return self._issue(
            common_name, self._load_public(public), valid_days, self._private_der(private), names
        )

    def revoke(self, serial: str) -> None:
        raise NotImplementedError("revocation lands with certificate rotation, Day 5-6")

    # -- internal -------------------------------------------------

    def _issue(
        self,
        subject_name: str,
        public_key_obj,
        valid_days: int,
        private_key_der: bytes | None,
        san_names: list[str] | None,
    ) -> IssuedCertificate:
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject_name)])
        now = datetime.datetime.now(datetime.timezone.utc)
        not_after = now + datetime.timedelta(days=valid_days)
        serial = x509.random_serial_number()

        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(self._root_name)
            .public_key(public_key_obj)
            .serial_number(serial)
            .not_valid_before(now)
            .not_valid_after(not_after)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key_obj), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self._root_certificate.public_key()),
                critical=False,
            )
        )
        if san_names:
            builder = builder.add_extension(
                x509.SubjectAlternativeName([x509.DNSName(n) for n in san_names]),
                critical=False,
            )

        certificate = builder.sign(self._signing_key(), self._signature_hash())

        return IssuedCertificate(
            certificate_der=certificate.public_bytes(serialization.Encoding.DER),
            private_key_der=private_key_der or b"",
            serial=format(serial, "x"),
            not_valid_after=not_after,
        )

    def _self_sign_root(self) -> x509.Certificate:
        now = datetime.datetime.now(datetime.timezone.utc)
        public_key_obj = self._load_public(self._root_public)
        builder = (
            x509.CertificateBuilder()
            .subject_name(self._root_name)
            .issuer_name(self._root_name)
            .public_key(public_key_obj)
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=ROOT_VALIDITY_DAYS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=False,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key_obj), critical=False)
        )
        return builder.sign(self._signing_key(), self._signature_hash())

    def _signature_hash(self):
        """SHA-256 for ECDSA; None for ML-DSA (pure ML-DSA signs the message
        itself, and cryptography requires algorithm=None for it)."""
        return None if self.mode == "pqc" else hashes.SHA256()

    def _signing_key(self):
        """Root private key as a native cryptography object for
        CertificateBuilder.sign()."""
        if self.mode == "pqc":
            return self._mldsa.MLDSA44PrivateKey.from_seed_bytes(self._root_private)
        return serialization.load_der_private_key(self._root_private, password=None)

    def _load_public(self, public_key: bytes):
        """Provider/station public-key bytes -> native public key object.
        SubjectPublicKeyInfo DER in both modes; raw ML-DSA-44 bytes in pqc."""
        if self.mode == "pqc":
            try:
                key = serialization.load_der_public_key(public_key)
            except ValueError:
                key = self._mldsa.MLDSA44PublicKey.from_public_bytes(public_key)
        else:
            key = serialization.load_der_public_key(public_key)
        if not self._key_matches_mode(key):
            raise ValueError(
                f"public key type {type(key).__name__} does not match CA mode {self.mode!r}"
            )
        return key

    def _private_der(self, private_key: bytes) -> bytes:
        """Provider private-key bytes -> PKCS8 DER (what store.py and
        ssl.load_cert_chain expect). Classical keys already are; the ML-DSA
        seed is wrapped."""
        if self.mode == "pqc":
            key = self._mldsa.MLDSA44PrivateKey.from_seed_bytes(private_key)
            return key.private_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        return private_key

    def _key_matches_mode(self, public_key) -> bool:
        if self.mode == "pqc":
            return isinstance(public_key, self._mldsa.MLDSA44PublicKey)
        from cryptography.hazmat.primitives.asymmetric import ec

        return isinstance(public_key, ec.EllipticCurvePublicKey)
