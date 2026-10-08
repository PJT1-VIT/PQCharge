"""Tests for the certificate authority: classical mode, post-quantum mode
(ML-DSA-44), strict-verification extensions (T6) and CSR issuance."""

import datetime

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import NameOID

from crypto.ca import CertificateAuthority
from crypto.classical import ClassicalProvider
from crypto.pq import PQProvider


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


def test_ca_rejects_hybrid_mode():
    # A hybrid fleet uses classical certificates with hybrid key exchange, so
    # there is no hybrid CA. Only the mode attribute is read before raising.
    class _HybridStub:
        mode = "hybrid"

    with pytest.raises(NotImplementedError):
        CertificateAuthority(_HybridStub())

# -- SAN tests (added when issue_server_certificate landed) --------------

def test_server_certificate_has_default_san():
    ca = CertificateAuthority(ClassicalProvider())
    issued = ca.issue_server_certificate()
    cert = x509.load_der_x509_certificate(issued.certificate_der)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    assert san.value.get_values_for_type(x509.DNSName) == ["localhost"]


def test_server_certificate_honours_custom_san_names():
    ca = CertificateAuthority(ClassicalProvider())
    issued = ca.issue_server_certificate(
        san_names=["localhost", "drs-macbook-air.local"]
    )
    cert = x509.load_der_x509_certificate(issued.certificate_der)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    assert san.value.get_values_for_type(x509.DNSName) == [
        "localhost",
        "drs-macbook-air.local",
    ]


def test_station_certificate_has_no_san_by_default():
    # Station (client) certificates are not hostname-verified, so they carry
    # no SAN unless one is explicitly requested.
    ca = CertificateAuthority(ClassicalProvider())
    issued = ca.issue_station_certificate_with_new_key("CP001")
    cert = x509.load_der_x509_certificate(issued.certificate_der)
    with pytest.raises(x509.ExtensionNotFound):
        cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)


def test_server_certificate_private_key_returned():
    ca = CertificateAuthority(ClassicalProvider())
    issued = ca.issue_server_certificate()
    assert issued.private_key_der != b""

# -- T6: extensions required by VERIFY_X509_STRICT (Python 3.13+ default) ----

def _ext(cert, cls):
    return cert.extensions.get_extension_for_class(cls)


@pytest.mark.parametrize("mode", ["classical", "pqc"])
def test_root_has_ski_keyusage_and_basic_constraints(mode):
    ca = CertificateAuthority(_provider(mode))
    root = x509.load_der_x509_certificate(ca.root_certificate_der)
    assert _ext(root, x509.BasicConstraints).value.ca is True
    ku = _ext(root, x509.KeyUsage)
    assert ku.critical and ku.value.key_cert_sign and ku.value.crl_sign
    assert _ext(root, x509.SubjectKeyIdentifier).value.digest


@pytest.mark.parametrize("mode", ["classical", "pqc"])
def test_leaves_have_ski_and_aki_pointing_at_the_root(mode):
    ca = CertificateAuthority(_provider(mode))
    root = x509.load_der_x509_certificate(ca.root_certificate_der)
    root_ski = _ext(root, x509.SubjectKeyIdentifier).value.digest
    for issued in (ca.issue_station_certificate_with_new_key("CP0001"),
                   ca.issue_server_certificate()):
        cert = x509.load_der_x509_certificate(issued.certificate_der)
        assert _ext(cert, x509.SubjectKeyIdentifier).value.digest
        assert _ext(cert, x509.AuthorityKeyIdentifier).value.key_identifier == root_ski


# -- post-quantum CA (ML-DSA-44) ------------------------------------------------

def _provider(mode):
    return ClassicalProvider() if mode == "classical" else PQProvider()


def test_pqc_root_is_self_signed_ml_dsa():
    from cryptography.hazmat.primitives.asymmetric import mldsa

    ca = CertificateAuthority(PQProvider())
    assert ca.mode == "pqc"
    root = x509.load_der_x509_certificate(ca.root_certificate_der)
    assert isinstance(root.public_key(), mldsa.MLDSA44PublicKey)
    assert root.subject == root.issuer
    root.public_key().verify(root.signature, root.tbs_certificate_bytes)  # raises if invalid


def test_pqc_station_certificate_verifies_against_root_and_key_is_pkcs8():
    from cryptography.hazmat.primitives.asymmetric import mldsa

    ca = CertificateAuthority(PQProvider())
    issued = ca.issue_station_certificate_with_new_key("CP0001")
    cert = x509.load_der_x509_certificate(issued.certificate_der)
    root = x509.load_der_x509_certificate(ca.root_certificate_der)
    root.public_key().verify(cert.signature, cert.tbs_certificate_bytes)
    assert cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "CP0001"
    key = serialization.load_der_private_key(issued.private_key_der, password=None)
    assert isinstance(key, mldsa.MLDSA44PrivateKey)
    assert key.public_key().public_bytes_raw() == cert.public_key().public_bytes_raw()


def test_pqc_issue_for_raw_provider_public_key():
    ca = CertificateAuthority(PQProvider())
    _, raw_pub = PQProvider().generate_keypair()   # raw 1312-byte ML-DSA key
    issued = ca.issue_station_certificate("CP0002", raw_pub)
    cert = x509.load_der_x509_certificate(issued.certificate_der)
    assert cert.public_key().public_bytes_raw() == raw_pub
    assert issued.private_key_der == b""


def test_pqc_server_certificate_size_fits_ocpp_limit():
    from crypto.store import measure_certificate_sizes

    ca = CertificateAuthority(PQProvider())
    sizes = measure_certificate_sizes(ca.issue_server_certificate().certificate_der)
    assert sizes.pem_bytes <= 5500, sizes  # OCPP 2.0.1 single-certificate limit


def test_ca_rejects_a_key_of_the_other_algorithm():
    pq_ca = CertificateAuthority(PQProvider())
    _, ec_pub = ClassicalProvider().generate_keypair()
    with pytest.raises(ValueError):
        pq_ca.issue_station_certificate("CP0003", ec_pub)


# -- CSR issuance (OCPP SignCertificate -> CertificateSigned) --------------------

def _csr(cn, key):
    from cryptography.hazmat.primitives.asymmetric import mldsa

    algo = None if isinstance(key, mldsa.MLDSA44PrivateKey) else hashes.SHA256()
    return (x509.CertificateSigningRequestBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
            .sign(key, algo)).public_bytes(serialization.Encoding.PEM)


def test_pqc_csr_is_issued_with_the_stations_own_key():
    from cryptography.hazmat.primitives.asymmetric import mldsa

    ca = CertificateAuthority(PQProvider())
    station_key = mldsa.MLDSA44PrivateKey.generate()      # made ON the station
    issued = ca.issue_certificate_from_csr(_csr("CP0007", station_key), "CP0007")
    cert = x509.load_der_x509_certificate(issued.certificate_der)
    assert cert.public_key().public_bytes_raw() == station_key.public_key().public_bytes_raw()
    assert issued.private_key_der == b"", "the CA never holds the station's private key"
    x509.load_der_x509_certificate(ca.root_certificate_der).public_key().verify(
        cert.signature, cert.tbs_certificate_bytes)


def test_csr_accepts_pem_text_and_der():
    from cryptography.hazmat.primitives.asymmetric import mldsa

    ca = CertificateAuthority(PQProvider())
    pem = _csr("CP0008", mldsa.MLDSA44PrivateKey.generate())
    der = x509.load_pem_x509_csr(pem).public_bytes(serialization.Encoding.DER)
    assert ca.issue_certificate_from_csr(pem.decode(), "CP0008").certificate_der
    assert ca.issue_certificate_from_csr(der, "CP0008").certificate_der


def test_csr_with_wrong_station_id_is_rejected():
    from cryptography.hazmat.primitives.asymmetric import mldsa
    from crypto.ca import CSRRejected

    ca = CertificateAuthority(PQProvider())
    with pytest.raises(CSRRejected):
        ca.issue_certificate_from_csr(_csr("CP0009", mldsa.MLDSA44PrivateKey.generate()), "CP0001")


def test_pqc_ca_rejects_a_classical_csr():
    from cryptography.hazmat.primitives.asymmetric import ec
    from crypto.ca import CSRRejected

    ca = CertificateAuthority(PQProvider())
    with pytest.raises(CSRRejected):
        ca.issue_certificate_from_csr(_csr("CP0010", ec.generate_private_key(ec.SECP256R1())), "CP0010")


def test_garbage_csr_is_rejected():
    from crypto.ca import CSRRejected

    ca = CertificateAuthority(PQProvider())
    with pytest.raises(CSRRejected):
        ca.issue_certificate_from_csr(b"not a csr", "CP0001")


def test_classical_csr_works_on_a_classical_ca():
    from cryptography.hazmat.primitives.asymmetric import ec

    ca = CertificateAuthority(ClassicalProvider())
    issued = ca.issue_certificate_from_csr(_csr("CP0011", ec.generate_private_key(ec.SECP256R1())), "CP0011")
    assert issued.certificate_der


# -- real TLS 1.3 mutual handshake under VERIFY_X509_STRICT ----------------------

def _strict_mutual_handshake(ca, tmp_path):
    import ssl
    from crypto.store import certificate_der_to_pem, save_certificate, save_private_key

    (tmp_path / "root.pem").write_bytes(certificate_der_to_pem(ca.root_certificate_der))
    server = ca.issue_server_certificate()
    save_certificate(server.certificate_der, "localhost", directory=tmp_path)
    save_private_key(server.private_key_der, "localhost", directory=tmp_path)
    station = ca.issue_station_certificate_with_new_key("CP0001")
    save_certificate(station.certificate_der, "CP0001", directory=tmp_path)
    save_private_key(station.private_key_der, "CP0001", directory=tmp_path)

    s = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    s.load_cert_chain(tmp_path / "localhost.crt.pem", tmp_path / "localhost.key.pem")
    s.load_verify_locations(tmp_path / "root.pem")
    s.verify_mode = ssl.CERT_REQUIRED
    s.verify_flags |= ssl.VERIFY_X509_STRICT
    c = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    c.load_cert_chain(tmp_path / "CP0001.crt.pem", tmp_path / "CP0001.key.pem")
    c.load_verify_locations(tmp_path / "root.pem")
    c.verify_flags |= ssl.VERIFY_X509_STRICT

    ci, co, si, so = ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO(), ssl.MemoryBIO()
    cs = c.wrap_bio(ci, co, server_hostname="localhost")
    ss = s.wrap_bio(si, so, server_side=True)
    done_c = done_s = False
    for _ in range(20):
        if not done_c:
            try:
                cs.do_handshake()
                done_c = True
            except ssl.SSLWantReadError:
                pass
        data = co.read()
        if data:
            si.write(data)
        if not done_s:
            try:
                ss.do_handshake()
                done_s = True
            except ssl.SSLWantReadError:
                pass
        data = so.read()
        if data:
            ci.write(data)
        if done_c and done_s:
            break
    assert done_c and done_s
    peer = dict(x[0] for x in ss.getpeercert()["subject"])
    return cs.version(), peer["commonName"]


def test_classical_certificates_pass_strict_mutual_tls(tmp_path):
    version, cn = _strict_mutual_handshake(CertificateAuthority(ClassicalProvider()), tmp_path)
    assert version == "TLSv1.3" and cn == "CP0001"


def test_pqc_certificates_pass_strict_mutual_tls(tmp_path):
    import ssl

    if ssl.OPENSSL_VERSION_INFO < (3, 5):
        pytest.skip(f"ML-DSA in TLS needs OpenSSL >= 3.5; this Python has {ssl.OPENSSL_VERSION}")
    version, cn = _strict_mutual_handshake(CertificateAuthority(PQProvider()), tmp_path)
    assert version == "TLSv1.3" and cn == "CP0001"


# -- reloading the CA from disk (the CSMS signs rotation CSRs with it) ----------

@pytest.mark.parametrize("mode", ["classical", "pqc"])
def test_ca_reloaded_from_files_issues_under_the_same_root(mode):
    from crypto.store import certificate_der_to_pem, private_key_der_to_pem

    original = CertificateAuthority(_provider(mode))
    reloaded = CertificateAuthority.from_files(
        _provider(mode),
        certificate_der_to_pem(original.root_certificate_der),
        private_key_der_to_pem(original.root_private_key_der),
    )
    assert reloaded.root_certificate_der == original.root_certificate_der
    cert = x509.load_der_x509_certificate(reloaded.issue_station_certificate_with_new_key("CP0001").certificate_der)
    root = x509.load_der_x509_certificate(original.root_certificate_der)
    if mode == "pqc":
        root.public_key().verify(cert.signature, cert.tbs_certificate_bytes)
    else:
        from cryptography.hazmat.primitives.asymmetric import ec
        root.public_key().verify(cert.signature, cert.tbs_certificate_bytes, ec.ECDSA(hashes.SHA256()))


def test_ca_reload_rejects_a_key_from_another_ca():
    from crypto.store import certificate_der_to_pem, private_key_der_to_pem

    a, b = CertificateAuthority(PQProvider()), CertificateAuthority(PQProvider())
    with pytest.raises(ValueError):
        CertificateAuthority.from_files(PQProvider(), certificate_der_to_pem(a.root_certificate_der),
                                        private_key_der_to_pem(b.root_private_key_der))
