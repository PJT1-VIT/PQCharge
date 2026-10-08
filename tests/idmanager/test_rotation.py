"""
Live certificate rotation (plan Phase 5) -- orchestrator + rotation driver,
with REAL keys, CSRs and certificates and a simulated fleet of chargers.

The simulated charger behaves as the plan requires a real one to: it makes its
own ML-DSA-44 key, answers TriggerMessage with a CSR, stores the certificate,
and on the CSMS's disconnect comes back with the new certificate -- or, if its
new handshake cannot complete, with the old one.
"""

from __future__ import annotations

import asyncio

import pytest

from crypto.ca import CertificateAuthority
from crypto.classical import ClassicalProvider
from crypto.csr import certificate_serial_hex, new_station_key_and_csr
from crypto.identity import MigrationState, StationIdentity
from crypto.pq import PQProvider
from crypto.store import certificate_pem_to_der
from idmanager.api import MigrationPhase
from idmanager.orchestrator import MigrationOrchestrator
from idmanager.rotation import (
    CLOSE_CODE_REFUSED,
    CLOSE_CODE_ROTATE,
    CertificateRotationDriver,
    RotationBroker,
)

ML_DSA = "ML-DSA-44"


class _Result:
    def __init__(self, ok, status=None):
        self.ok = ok
        self.outcome = "success" if ok else "rejected"
        self.status = status


class _SimulatedFleet:
    """Chargers + the CSMS's dispatcher/close, wired to a real RotationBroker.

    behaviours: normal | reject_cert | broken_pq | silent (never sends a CSR)
                | wrong_cn (CSR for another station id)
    """

    def __init__(self, broker, behaviours, classical_ca):
        self.broker = broker
        self.behaviours = behaviours
        self.ecdsa_der = {}
        self.pending_key = {}
        self.new_der = {}
        self.closes = []
        self.requests = []
        for sid in behaviours:
            der = classical_ca.issue_station_certificate_with_new_key(sid).certificate_der
            self.ecdsa_der[sid] = der
            assert broker.on_connected(sid, der)

    async def send(self, station_id, request, *, timeout_s=None):
        self.requests.append((station_id, request[0]))
        behaviour = self.behaviours[station_id]
        await asyncio.sleep(0)
        if request[0] == "TriggerMessage":
            if behaviour != "silent":
                cn = "CP-OTHER" if behaviour == "wrong_cn" else station_id
                key_pem, csr = new_station_key_and_csr(cn)
                self.pending_key[station_id] = key_pem
                asyncio.get_running_loop().call_soon(self.broker.on_sign_certificate, station_id, csr)
            return _Result(True, "Accepted")
        if request[0] == "CertificateSigned":
            if behaviour == "reject_cert":
                return _Result(False, "Rejected")
            self.new_der[station_id] = certificate_pem_to_der(request[1].encode())
            return _Result(True, "Accepted")
        raise AssertionError(f"unexpected request {request!r}")

    async def close(self, station_id, code, reason):
        self.closes.append((station_id, code))
        await asyncio.sleep(0)
        behaviour = self.behaviours[station_id]
        new = self.new_der.get(station_id)
        # The charger reconnects: new certificate first, unless its PQ
        # handshake cannot complete; refused -> it falls back to ECDSA.
        if new is not None and behaviour != "broken_pq" and self.broker.on_connected(station_id, new):
            return
        self.broker.on_connected(station_id, self.ecdsa_der[station_id])


class _Fleet:
    def __init__(self, caps):
        self.ids = {sid: StationIdentity(sid, "ECDSA-P256", supported_algorithms=a) for sid, a in caps.items()}

    def migration_candidate_ids(self):
        return sorted(self.ids)

    def supported_algorithms(self, sid):
        return list(self.ids[sid].supported_algorithms)

    def set_state(self, sid, state, wave):
        self.ids[sid].migration_state = state
        self.ids[sid].migration_wave = wave

    def mark_migrated_algorithm(self, sid, alg):
        self.ids[sid].current_algorithm = alg

    def record_certificate(self, sid, serial, previous):
        self.ids[sid].certificate_serial = serial
        self.ids[sid].previous_certificate_serial = previous


def _build(behaviours, *, caps=None, threshold=0.2, events=None):
    broker = RotationBroker()
    sim = _SimulatedFleet(broker, behaviours, CertificateAuthority(ClassicalProvider()))
    driver = CertificateRotationDriver(
        dispatcher=sim,
        broker=broker,
        ca=CertificateAuthority(PQProvider()),
        trigger_message_factory=lambda: ("TriggerMessage",),
        certificate_signed_factory=lambda chain: ("CertificateSigned", chain),
        close_connection=sim.close,
        step_timeout_s=0.5,
        reconnect_timeout_s=0.5,
    )
    fleet = _Fleet(caps or {sid: ["ECDSA-P256", ML_DSA] for sid in behaviours})
    emit = (lambda ev, **k: events.append((ev, k))) if events is not None else None

    def _must_not_be_used(*_a, **_k):
        raise AssertionError("Option B enrolment must not run during rotation")

    orch = MigrationOrchestrator(
        dispatcher=sim,
        authenticator=None,
        fleet=fleet,
        keypair_factory=_must_not_be_used,
        install_message_factory=_must_not_be_used,
        event_emitter=emit,
        failure_threshold=threshold,
        rotation_driver=driver,
    )
    return orch, fleet, sim, broker


async def _finish(orch):
    for _ in range(2000):
        status = orch.get_migration_status()
        if status.is_terminal:
            await asyncio.sleep(0.01)  # let scheduled rollback closes run
            return status
        await asyncio.sleep(0.005)
    raise AssertionError("migration did not finish")


def _counts_sum(status):
    return (status.migrated + status.rolled_back + status.incompatible + status.pending
            == status.total_stations)


@pytest.mark.asyncio
async def test_clean_rotation_moves_every_charger_to_its_own_ml_dsa_certificate():
    events = []
    behaviours = {f"CP{i:04d}": "normal" for i in range(1, 6)}
    orch, fleet, sim, broker = _build(behaviours, events=events)
    orch.start_migration(wave_size=2, canary_count=1, target_mode="pqc")
    status = await _finish(orch)

    assert status.phase == MigrationPhase.COMPLETED and status.migrated == 5
    assert _counts_sum(status)
    for sid in behaviours:
        ident = fleet.ids[sid]
        assert ident.migration_state == MigrationState.MIGRATED
        assert ident.current_algorithm == ML_DSA
        # Contract 2: the new serial is current, the ECDSA one is "previous".
        assert ident.certificate_serial == certificate_serial_hex(sim.new_der[sid])
        assert ident.previous_certificate_serial == certificate_serial_hex(sim.ecdsa_der[sid])
        assert broker.current_serial(sid) == ident.certificate_serial
        assert (sid, CLOSE_CODE_ROTATE) in sim.closes          # the CSMS triggered the reconnect
    # Only the two OCPP certificate messages crossed the wire: no private key.
    assert {kind for _, kind in sim.requests} == {"TriggerMessage", "CertificateSigned"}
    rotations = [k for ev, k in events if ev == "connection_attempt"]
    assert len(rotations) == 5 and all(k["transition"] == "cert_rotation" and k["result"] == "success"
                                       for k in rotations)
    started = [k for ev, k in events if ev == "migration_started"][0]
    assert started["method"] == "cert_rotation"


@pytest.mark.asyncio
async def test_charger_rejecting_the_certificate_is_rolled_back_and_keeps_ecdsa():
    behaviours = {"CP0001": "normal", "CP0002": "normal", "CP0003": "normal", "CP0004": "reject_cert"}
    orch, fleet, sim, broker = _build(behaviours, threshold=0.5)
    orch.start_migration(wave_size=3, canary_count=1, target_mode="pqc")
    status = await _finish(orch)

    assert status.phase == MigrationPhase.COMPLETED      # 1/3 failed <= 0.5
    assert fleet.ids["CP0004"].migration_state == MigrationState.ROLLED_BACK
    assert broker.current_serial("CP0004") == certificate_serial_hex(sim.ecdsa_der["CP0004"])
    assert status.migrated == 3 and status.rolled_back == 1 and _counts_sum(status)


@pytest.mark.asyncio
async def test_failed_pq_handshake_falls_back_and_the_new_serial_is_refused():
    events = []
    behaviours = {"CP0001": "normal", "CP0002": "broken_pq"}
    orch, fleet, sim, broker = _build(behaviours, threshold=0.9, events=events)
    orch.start_migration(wave_size=1, canary_count=1, target_mode="pqc")
    status = await _finish(orch)

    assert fleet.ids["CP0002"].migration_state == MigrationState.ROLLED_BACK
    new_serial = certificate_serial_hex(sim.new_der["CP0002"])
    assert broker.is_refused("CP0002", new_serial)
    assert broker.current_serial("CP0002") == certificate_serial_hex(sim.ecdsa_der["CP0002"])
    detail = [k for ev, k in events if ev == "connection_attempt" and k["station"] == "CP0002"][0]
    assert detail["step"] == "reconnect" and "fell back" in detail["detail"]
    assert _counts_sum(status)


@pytest.mark.asyncio
async def test_silent_charger_times_out_at_the_csr_step():
    events = []
    orch, fleet, sim, broker = _build({"CP0001": "silent"}, threshold=1.0, events=events)
    orch.start_migration(wave_size=1, canary_count=1, target_mode="pqc")
    await _finish(orch)
    detail = [k for ev, k in events if ev == "connection_attempt"][0]
    assert detail["result"] == "rejected" and detail["step"] == "csr"
    assert fleet.ids["CP0001"].migration_state == MigrationState.ROLLED_BACK


@pytest.mark.asyncio
async def test_csr_for_another_station_is_refused():
    events = []
    orch, fleet, sim, broker = _build({"CP0001": "wrong_cn"}, threshold=1.0, events=events)
    orch.start_migration(wave_size=1, canary_count=1, target_mode="pqc")
    await _finish(orch)
    detail = [k for ev, k in events if ev == "connection_attempt"][0]
    assert detail["step"] == "issue" and "CSR refused" in detail["detail"]
    assert "CertificateSigned" not in {kind for _, kind in sim.requests}


@pytest.mark.asyncio
async def test_failed_wave_rolls_back_its_rotated_chargers_to_ecdsa():
    # Canary OK; wave 1 = CP0002..CP0004 with two refusers -> 2/3 > 0.2 -> rollback.
    behaviours = {"CP0001": "normal", "CP0002": "normal", "CP0003": "reject_cert", "CP0004": "reject_cert"}
    orch, fleet, sim, broker = _build(behaviours, threshold=0.2)
    orch.start_migration(wave_size=3, canary_count=1, target_mode="pqc")
    status = await _finish(orch)

    assert status.phase == MigrationPhase.ROLLED_BACK
    assert _counts_sum(status)
    # CP0002 had rotated; the wave rollback refused its new serial and
    # disconnected it, so it came back on its ECDSA certificate.
    rotated_serial = certificate_serial_hex(sim.new_der["CP0002"])
    assert broker.is_refused("CP0002", rotated_serial)
    assert ("CP0002", CLOSE_CODE_REFUSED) in sim.closes
    assert broker.current_serial("CP0002") == certificate_serial_hex(sim.ecdsa_der["CP0002"])
    assert fleet.ids["CP0002"].migration_state == MigrationState.ROLLED_BACK
    assert fleet.ids["CP0002"].certificate_serial == certificate_serial_hex(sim.ecdsa_der["CP0002"])
    # The canary stays migrated: only the failed wave is reverted.
    assert fleet.ids["CP0001"].migration_state == MigrationState.MIGRATED


@pytest.mark.asyncio
async def test_incompatible_charger_is_skipped_not_rotated():
    behaviours = {"CP0001": "normal", "CP0002": "normal"}
    caps = {"CP0001": ["ECDSA-P256", ML_DSA], "CP0002": ["ECDSA-P256"]}
    orch, fleet, sim, broker = _build(behaviours, caps=caps)
    orch.start_migration(wave_size=1, canary_count=1, target_mode="pqc")
    status = await _finish(orch)
    assert fleet.ids["CP0002"].migration_state == MigrationState.INCOMPATIBLE
    assert ("CP0002", "TriggerMessage") not in sim.requests
    assert status.incompatible == 1 and _counts_sum(status)


def test_driver_requires_a_post_quantum_ca():
    with pytest.raises(ValueError):
        CertificateRotationDriver(
            dispatcher=None, broker=RotationBroker(), ca=CertificateAuthority(ClassicalProvider()),
            trigger_message_factory=lambda: None, certificate_signed_factory=lambda c: None,
            close_connection=None,
        )


def test_broker_rejects_unsolicited_csrs_and_refused_certificates():
    broker = RotationBroker()
    assert broker.on_sign_certificate("CP0001", "-----BEGIN CERTIFICATE REQUEST-----") is False
    der = CertificateAuthority(ClassicalProvider()).issue_station_certificate_with_new_key("CP0001").certificate_der
    assert broker.on_connected("CP0001", der) is True
    broker.refuse("CP0001", certificate_serial_hex(der))
    assert broker.on_connected("CP0001", der) is False
