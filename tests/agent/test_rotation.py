"""
The station's half of live certificate rotation (plan Phase 5).

No network: the TLS station is built from a real bootstrap-layout PKI in a
temporary directory, and the rotation steps are driven directly -- the
TriggerMessage gate, the CSR (key made on the station), CertificateSigned
acceptance/refusal, persistence, and the switch / fall-back decision the
reconnect loop takes.
"""

from __future__ import annotations

import ssl

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.frames import Close

from agent.config import AgentConfig
from agent.station import ChargingStation
from crypto.ca import CertificateAuthority
from crypto.classical import ClassicalProvider
from crypto.pq import PQProvider
from crypto.store import certificate_der_to_pem, save_certificate, save_private_key
from crypto.tls_mode import PQ_SERVER_NAME


@pytest.fixture
def pki(tmp_path):
    """certs/ (ECDSA) + roots_all.pem, and the ML-DSA CA that will sign."""
    certs = tmp_path / "certs"
    certs.mkdir()
    ec_ca = CertificateAuthority(ClassicalProvider())
    pq_ca = CertificateAuthority(PQProvider())
    (certs / "root.pem").write_bytes(certificate_der_to_pem(ec_ca.root_certificate_der))
    (certs / "roots_all.pem").write_bytes(
        certificate_der_to_pem(ec_ca.root_certificate_der) + certificate_der_to_pem(pq_ca.root_certificate_der))
    station = ec_ca.issue_station_certificate_with_new_key("CP0001")
    save_certificate(station.certificate_der, "CP0001", directory=certs)
    save_private_key(station.private_key_der, "CP0001", directory=certs)
    return {"certs": certs, "rotated": tmp_path / "rotated", "pq_ca": pq_ca, "ec_ca": ec_ca}


def _station(pki, **overrides):
    config = dict(station_id="CP0001", csms_url="wss://localhost:9000",
                  cert_dir=str(pki["certs"]), rotation_dir=str(pki["rotated"]),
                  crypto_mode="classical")
    config.update(overrides)
    return ChargingStation(AgentConfig(**config))


def _issue_for_pending_key(station, pki, cn="CP0001"):
    """What the CSMS does with the station's CSR: sign it with the ML-DSA CA."""
    from crypto.csr import new_station_key_and_csr

    key_pem, csr = new_station_key_and_csr(cn)
    station._pending_rotation_key = key_pem
    issued = pki["pq_ca"].issue_certificate_from_csr(csr, cn)
    return certificate_der_to_pem(issued.certificate_der).decode()


def test_trigger_is_accepted_over_tls_and_refused_without_ml_dsa_support(pki):
    assert _station(pki).handle_trigger_message("SignChargingStationCertificate", None)[0]
    legacy = _station(pki, supported_algorithms=["ECDSA-P256"])
    ok, reason = legacy.handle_trigger_message("SignChargingStationCertificate", None)
    assert not ok and "ML-DSA-44" in reason
    plain = ChargingStation(AgentConfig(station_id="CP0001", csms_url="ws://localhost:9000"))
    assert not plain.handle_trigger_message("SignChargingStationCertificate", None)[0]


def test_certificate_for_the_stations_own_key_is_stored_and_old_pair_kept(pki):
    station = _station(pki)
    old_context = station._ssl_context
    ok, reason = station.handle_certificate_signed(_issue_for_pending_key(station, pki), None)
    assert ok, reason
    assert (pki["rotated"] / "CP0001.crt.pem").is_file()
    assert (pki["rotated"] / "CP0001.key.pem").is_file()
    assert (pki["certs"] / "CP0001.crt.pem").is_file(), "fallback pair untouched"
    assert station._rotation.phase == "installed"
    assert station._ssl_context is old_context, "switch happens on the NEXT connection"


def test_certificate_without_an_outstanding_request_is_refused(pki):
    station = _station(pki)
    chain = _issue_for_pending_key(station, pki)
    station._pending_rotation_key = None
    assert station.handle_certificate_signed(chain, None) == (False, "no certificate request is outstanding")


def test_certificate_for_another_key_is_refused(pki):
    station = _station(pki)
    chain = _issue_for_pending_key(station, pki)
    from crypto.csr import new_station_key_and_csr

    station._pending_rotation_key = new_station_key_and_csr("CP0001")[0]   # a different key
    ok, reason = station.handle_certificate_signed(chain, None)
    assert not ok and "does not match" in reason


def test_certificate_for_another_station_is_refused(pki):
    station = _station(pki)
    ok, reason = station.handle_certificate_signed(_issue_for_pending_key(station, pki, cn="CP0099"), None)
    assert not ok and "not issued to this station" in reason


def test_csms_close_after_install_switches_to_the_new_certificate(pki):
    station = _station(pki)
    assert station.handle_certificate_signed(_issue_for_pending_key(station, pki), None)[0]
    station._last_connection_error = ConnectionClosed(Close(1012, "certificate rotated"), None)
    assert station._rotation_reconnect_now() is True          # immediate, no backoff
    assert station._rotation.phase == "switching"
    assert station._ssl_context is station._rotation.new_context


def test_refused_new_certificate_falls_back_to_the_previous_one(pki):
    station = _station(pki)
    old_context = station._ssl_context
    station.handle_certificate_signed(_issue_for_pending_key(station, pki), None)
    station._rotation_reconnect_now()                          # -> switching
    station._last_connection_error = ConnectionClosed(Close(1008, "certificate refused: rolled back"), None)
    assert station._rotation_reconnect_now() is True
    assert station._rotation.phase == "fell_back"
    assert station._ssl_context is old_context


def test_tls_handshake_failure_on_the_new_certificate_falls_back(pki):
    station = _station(pki)
    station.handle_certificate_signed(_issue_for_pending_key(station, pki), None)
    station._rotation_reconnect_now()
    station._last_connection_error = ssl.SSLError("handshake failure")
    assert station._rotation_reconnect_now() is True and station._rotation.phase == "fell_back"


def test_an_outage_does_not_undo_a_rotation(pki):
    """E2: the CSMS going away is not a reason to drop the new certificate."""
    station = _station(pki)
    station.handle_certificate_signed(_issue_for_pending_key(station, pki), None)
    station._rotation_reconnect_now()
    station._rotation.phase = "active"
    station._last_connection_error = ConnectionRefusedError()
    assert station._rotation_reconnect_now() is False          # normal backoff, keep new cert
    assert station._rotation.phase == "active"


def test_rotated_certificate_survives_a_restart(pki):
    station = _station(pki)
    station.handle_certificate_signed(_issue_for_pending_key(station, pki), None)
    restarted = _station(pki)                                   # finding F3
    assert restarted._rotation is not None and restarted._rotation.phase == "switching"
    assert restarted._ssl_context is restarted._rotation.new_context


def test_pq_identity_asks_for_the_csms_post_quantum_name():
    assert PQ_SERVER_NAME == "pq.localhost"
