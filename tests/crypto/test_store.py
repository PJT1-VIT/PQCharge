"""Tests for certificate/key persistence and DER/PEM conversion."""

import shutil
from pathlib import Path

import pytest
from cryptography import x509

from crypto.ca import CertificateAuthority
from crypto.classical import ClassicalProvider
from crypto.store import (
    certificate_der_to_pem,
    certificate_pem_to_der,
    private_key_der_to_pem,
    private_key_pem_to_der,
    save_certificate,
    save_private_key,
    load_certificate,
    load_private_key,
    measure_certificate_sizes,
    measure_chain_sizes,
)

TEST_DIR = Path("tests/_scratch_certs")


@pytest.fixture(autouse=True)
def clean_scratch_dir():
    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR)
    yield
    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR)


@pytest.fixture
def issued_cert():
    ca = CertificateAuthority(ClassicalProvider())
    return ca.issue_station_certificate_with_new_key("CP001")


def test_certificate_der_pem_roundtrip(issued_cert):
    pem = certificate_der_to_pem(issued_cert.certificate_der)
    assert pem.startswith(b"-----BEGIN CERTIFICATE-----")
    back_to_der = certificate_pem_to_der(pem)
    assert back_to_der == issued_cert.certificate_der


def test_private_key_der_pem_roundtrip(issued_cert):
    pem = private_key_der_to_pem(issued_cert.private_key_der)
    assert pem.startswith(b"-----BEGIN PRIVATE KEY-----")
    back_to_der = private_key_pem_to_der(pem)
    assert back_to_der == issued_cert.private_key_der


def test_save_and_load_certificate_roundtrip(issued_cert):
    save_certificate(issued_cert.certificate_der, "CP001", directory=TEST_DIR)
    loaded = load_certificate("CP001", directory=TEST_DIR)
    assert loaded == issued_cert.certificate_der


def test_save_and_load_private_key_roundtrip(issued_cert):
    save_private_key(issued_cert.private_key_der, "CP001", directory=TEST_DIR)
    loaded = load_private_key("CP001", directory=TEST_DIR)
    assert loaded == issued_cert.private_key_der


def test_loaded_certificate_still_parses(issued_cert):
    save_certificate(issued_cert.certificate_der, "CP001", directory=TEST_DIR)
    loaded_der = load_certificate("CP001", directory=TEST_DIR)
    cert = x509.load_der_x509_certificate(loaded_der)
    assert cert.serial_number == int(issued_cert.serial, 16)


def test_pem_is_larger_than_der(issued_cert):
    sizes = measure_certificate_sizes(issued_cert.certificate_der, label="ECDSA-P256")
    assert sizes.pem_bytes > sizes.der_bytes
    # Base64 alone is ~33% inflation before header/footer lines.
    assert sizes.inflation_ratio > 1.3


def test_chain_sizes_sum_correctly():
    ca = CertificateAuthority(ClassicalProvider())
    cert1 = ca.issue_station_certificate_with_new_key("CP001").certificate_der
    cert2 = ca.issue_station_certificate_with_new_key("CP002").certificate_der

    chain_sizes = measure_chain_sizes([cert1, cert2], label="two-cert chain")
    individual_der_total = len(cert1) + len(cert2)
    assert chain_sizes.der_bytes == individual_der_total