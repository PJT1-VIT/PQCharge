"""Tests for the certificate authority, classical mode."""

import datetime

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import NameOID

from crypto.ca import CertificateAuthority
from crypto.classical import ClassicalProvider
from crypto.pq_placeholder_for_tests import FakeNonClassicalProvider


def test_root_certificate_is_self_signed_and_valid():
    ca = CertificateAuthority(ClassicalProvider())
    root_der = ca.root_certificate_der
    root_cert = x509.load_der_x509_certificate(root_der)

    assert root_cert.subject == root_cert.issuer
    root_cert.public_key().verify(
        root_cert.signature,
        root_cert.tbs_certificate_bytes,
        __import__("cryptography.hazmat.primitives.asymmetric.ec", fromlist=["ECDSA"]).ECDSA(hashes.SHA256()),
    )  # raises if invalid; reaching the next line means it verified


def test_issue_with_new_key_produces_valid_certificate():
    ca = CertificateAuthority(ClassicalProvider())
    issued = ca.issue_station_certificate_with_new_key("CP001")

    cert = x509.load_der_x509_certificate(issued.certificate_der)
    assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "CP001"
    assert issued.private_key_der != b""

    assert format(cert.serial_number, "x") == issued.serial
    # X.509 encodes time to one-second resolution; the certificate's
    # own field is therefore truncated relative to the Python datetime
    # computed before encoding. Compare with a one-second tolerance
    # rather than exact equality.
    delta = abs((cert.not_valid_after_utc - issued.not_valid_after).total_seconds())
    assert delta < 1.0


def test_issued_certificate_signature_verifies_against_root():
    ca = CertificateAuthority(ClassicalProvider())
    issued = ca.issue_station_certificate_with_new_key("CP002")
    cert = x509.load_der_x509_certificate(issued.certificate_der)
    root_cert = x509.load_der_x509_certificate(ca.root_certificate_der)

    from cryptography.hazmat.primitives.asymmetric import ec

    root_cert.public_key().verify(
        cert.signature, cert.tbs_certificate_bytes, ec.ECDSA(hashes.SHA256())
    )  # raises InvalidSignature if the chain doesn't hold


def test_issue_for_externally_supplied_public_key():
    ca = CertificateAuthority(ClassicalProvider())
    provider = ClassicalProvider()
    _, station_pub_der = provider.generate_keypair()

    issued = ca.issue_station_certificate("CP003", station_pub_der)
    cert = x509.load_der_x509_certificate(issued.certificate_der)
    assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "CP003"
    # Caller supplied the key; the CA does not also hand back a private key.
    assert issued.private_key_der == b""


def test_expiry_respects_valid_days():
    ca = CertificateAuthority(ClassicalProvider())
    issued = ca.issue_station_certificate_with_new_key("CP004", valid_days=30)
    now = datetime.datetime.now(datetime.timezone.utc)
    delta = issued.not_valid_after - now
    assert 29 <= delta.days <= 30


def test_revoke_raises_not_implemented_until_day5():
    ca = CertificateAuthority(ClassicalProvider())
    with pytest.raises(NotImplementedError):
        ca.revoke("deadbeef")


def test_ca_rejects_non_classical_provider_for_now():
    with pytest.raises(NotImplementedError):
        CertificateAuthority(FakeNonClassicalProvider())