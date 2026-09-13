"""
Certificate authority.

Issues X.509 certificates for charging stations, signed by a single
root key. Certificates and their private keys are returned as DER
bytes, consistent with Contract 1's raw-bytes boundary; PEM conversion
happens only in crypto/store.py, at the OCPP edge.

DESIGN NOTE — why signing does not go through CryptoProvider.sign()
directly for the X.509 step:

cryptography's CertificateBuilder.sign() requires a native key object
(one of its own EC/RSA/Ed25519/etc. classes) and refuses anything
duck-typed -- this was verified directly against this project's
installed cryptography version before writing this file; a wrapper
object implementing sign()/public_key() raises TypeError rather than
being accepted.

The resolution used here: CryptoProvider.generate_keypair() still
produces the key material (respecting Contract 1), but for classical
mode those DER bytes are reloaded via
cryptography.hazmat.primitives.serialization.load_der_private_key(),
which returns a genuine ec.EllipticCurvePrivateKey object -- because
the DER bytes really do encode a native EC key, this reload is exact,
not an approximation. That native object is what actually signs the
certificate, via cryptography's own internals.

This holds for classical mode without qualification. It does NOT hold
for the post-quantum provider arriving Day 8: this installed
cryptography version has no Python type for an ML-DSA key at all, so
there is nothing to reload the DER bytes into. See _sign_root() below
for exactly where that breaks and the two real options for Day 8,
recorded here and in docs/limitations.md rather than guessed at now.
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


@dataclass
class IssuedCertificate:
    """
    Result of issuing a certificate.

    serial and not_valid_after are read directly off the certificate
    object that was actually built and signed, never computed
    separately -- so they cannot drift from what a verifier would read
    out of certificate_der. Track A's StationIdentity.certificate_serial
    and .certificate_expiry (Contract 2) are populated from these two
    fields.
    """

    certificate_der: bytes
    private_key_der: bytes
    serial: str
    not_valid_after: datetime.datetime


class CertificateAuthority:
    """
    Single-root CA for the demo fleet.

    One instance per CSMS process. The root keypair is generated once,
    at construction, via the supplied provider, and held for the
    process lifetime.
    """

    def __init__(
        self,
        provider: CryptoProvider,
        common_name: str = "PQCharge Root CA",
    ) -> None:
        if provider.mode != "classical":
            raise NotImplementedError(
                f"CertificateAuthority signing is only implemented for "
                f"classical mode (Day 3-6 scope); got mode={provider.mode!r}. "
                f"See the module docstring and docs/limitations.md for the "
                f"Day 8 post-quantum signing plan."
            )
        self._provider = provider
        self._root_name = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, common_name)]
        )
        self._root_private_der, self._root_public_der = provider.generate_keypair()
        self._root_certificate = self._self_sign_root()

    @property
    def mode(self) -> str:
        return self._provider.mode

    @property
    def root_certificate_der(self) -> bytes:
        """The CA's own self-signed certificate, for distribution to stations."""
        return self._root_certificate.public_bytes(serialization.Encoding.DER)

    def issue_station_certificate_with_new_key(
        self,
        station_id: str,
        valid_days: int = DEFAULT_VALIDITY_DAYS,
    ) -> IssuedCertificate:
        """
        Generate a fresh station keypair via this CA's provider and
        issue a certificate for it. The common path for the demo fleet
        and for certificate rotation, where a station needs a brand
        new identity.
        """
        private_der, public_der = self._provider.generate_keypair()
        return self._issue(station_id, public_der, valid_days, private_der)

    def issue_station_certificate(
        self,
        station_id: str,
        station_public_key_der: bytes,
        valid_days: int = DEFAULT_VALIDITY_DAYS,
    ) -> IssuedCertificate:
        """
        Issue a certificate for a station's own, already-generated
        keypair -- the path Track A's SignCertificate handler will use
        once a station submits its own public key rather than having
        one generated for it here.
        """
        return self._issue(station_id, station_public_key_der, valid_days, None)

    def revoke(self, serial: str) -> None:
        """
        Mark a certificate as revoked.

        Deferred to Day 5-6: revocation is built alongside rotation,
        once StationIdentity.previous_certificate_serial (Contract 2)
        gives it something concrete to revoke against. Left as an
        explicit stub rather than omitted, so the CA's eventual
        revocation surface is visible from Day 3.
        """
        raise NotImplementedError("revocation lands with certificate rotation, Day 5-6")

    # -- internal -------------------------------------------------

    def _issue(
        self,
        station_id: str,
        public_key_der: bytes,
        valid_days: int,
        private_key_der: bytes | None,
    ) -> IssuedCertificate:
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, station_id)])
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
            .add_extension(
                x509.BasicConstraints(ca=True, path_length=None), critical=True
            )
        )
        return builder.sign(self._signing_key(), hashes.SHA256())

    def _signing_key(self):
        """
        Return the root private key as a native cryptography object,
        for use with CertificateBuilder.sign().

        DAY 8: this method is the one place the post-quantum swap
        breaks. self._root_private_der would hold ML-DSA key bytes,
        and load_der_private_key() has no ML-DSA type to reload them
        into -- verified against this project's installed cryptography
        version, which defines no ml_dsa module. Two real options,
        recorded in docs/limitations.md rather than guessed at here:

          (a) hand-roll the certificate's ASN.1 structure with a
              library that permits an arbitrary signature algorithm
              OID and arbitrary signature bytes (e.g. asn1crypto),
              calling CryptoProvider.sign() directly on the encoded
              tbsCertificate and embedding the result; or

          (b) adopt this project's already-planned application-layer
              transport fallback for the post-quantum path, which
              carries the PQC handshake as OCPP messages instead of
              requiring a PQC-signed X.509 certificate at all.

        The __init__ guard above raises before this method is ever
        reached in non-classical mode, so this docstring is the
        record of the decision to be made, not a silent gap.
        """
        return serialization.load_der_private_key(
            self._root_private_der, password=None
        )