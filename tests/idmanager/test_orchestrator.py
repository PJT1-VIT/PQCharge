"""
Orchestrator tests — the pytest form of the rotation harness.

Proves the Contract 4 controller end to end with real ML-DSA against a fake
dispatcher and a fake fleet mirroring Contract 6. These are the assertions
behind E3 (migration under load) and the rollback / overlap-safety claims.
"""

import asyncio

import pytest

from crypto.pq import PQProvider
from crypto.pq_auth import PQAuthenticator
from crypto.identity import StationIdentity, MigrationState
from idmanager.orchestrator import MigrationOrchestrator
from idmanager.api import MigrationPhase


class _FakeResult:
    def __init__(self, ok):
        self.ok = ok
        self.outcome = "success" if ok else "failure"


class _FakeDispatcher:
    def __init__(self, fail_ids=None):
        self.fail_ids = set(fail_ids or [])
        self.sent = []

    async def send(self, station_id, request, *, timeout_s=None):
        self.sent.append(station_id)
        await asyncio.sleep(0)
        return _FakeResult(ok=station_id not in self.fail_ids)


class _FakeFleet:
    def __init__(self, caps):
        self._ids = {
            sid: StationIdentity(station_id=sid, current_algorithm="ECDSA-P256",
                                 supported_algorithms=algs)
            for sid, algs in caps.items()
        }

    def list_station_ids(self):
        return sorted(self._ids)

    def get_identity(self, sid):
        return self._ids.get(sid)

    def set_identity(self, it):
        if it.station_id not in self._ids:
            raise KeyError(it.station_id)
        self._ids[it.station_id] = it


class _Adapter:
    def __init__(self, fleet):
        self._f = fleet

    def migration_candidate_ids(self):
        return self._f.list_station_ids()

    def supported_algorithms(self, sid):
        it = self._f.get_identity(sid)
        return list(it.supported_algorithms) if it else []

    def set_state(self, sid, state, wave):
        it = self._f.get_identity(sid)
        if it is None:
            return
        it.migration_state = state
        it.migration_wave = wave
        try:
            self._f.set_identity(it)
        except KeyError:
            pass

    def mark_migrated_algorithm(self, sid, alg):
        it = self._f.get_identity(sid)
        if it is None:
            return
        it.current_algorithm = alg
        try:
            self._f.set_identity(it)
        except KeyError:
            pass


def _build(caps, *, fail_ids=None, events=None):
    provider = PQProvider()
    fleet = _FakeFleet(caps)
    auth = PQAuthenticator(provider)
    emitter = (lambda ev, **k: events.append(ev)) if events is not None else None
    orch = MigrationOrchestrator(
        dispatcher=_FakeDispatcher(fail_ids=fail_ids),
        authenticator=auth,
        fleet=_Adapter(fleet),
        keypair_factory=provider.generate_keypair,
        install_message_factory=lambda sid, priv: ("InstallPQAuth", sid),
        event_emitter=emitter,
    )
    return orch, fleet, auth


async def _run(orch):
    while not orch.get_migration_status().is_terminal:
        await asyncio.sleep(0.005)
    return orch.get_migration_status()


@pytest.mark.asyncio
async def test_clean_migration_all_capable():
    caps = {f"CP{i:03d}": ["ECDSA-P256", "ML-DSA-44"] for i in range(10)}
    orch, fleet, auth = _build(caps)
    orch.start_migration(wave_size=3, canary_count=2, target_mode="pqc")
    st = await _run(orch)
    assert st.phase == MigrationPhase.COMPLETED
    assert st.migrated == 10
    assert all(fleet.get_identity(s).migration_state == MigrationState.MIGRATED
               for s in caps)
    assert all(fleet.get_identity(s).current_algorithm == "ML-DSA-44" for s in caps)
    assert all(auth.is_enrolled(s) for s in caps)


@pytest.mark.asyncio
async def test_incompatible_stations_skipped_not_failed():
    caps = {f"CP{i:03d}": (["ECDSA-P256", "ML-DSA-44"] if i % 3 else ["ECDSA-P256"])
            for i in range(9)}
    orch, fleet, auth = _build(caps)
    orch.start_migration(wave_size=3, canary_count=2, target_mode="pqc")
    st = await _run(orch)
    assert st.phase == MigrationPhase.COMPLETED
    assert st.incompatible == 3
    incompatible = [s for s in caps
                    if fleet.get_identity(s).migration_state == MigrationState.INCOMPATIBLE]
    assert len(incompatible) == 3
    assert not any(auth.is_enrolled(s) for s in incompatible)


@pytest.mark.asyncio
async def test_canary_failure_rolls_back():
    caps = {f"CP{i:03d}": ["ECDSA-P256", "ML-DSA-44"] for i in range(10)}
    orch, fleet, auth = _build(caps, fail_ids={"CP000", "CP001"})
    orch.start_migration(wave_size=3, canary_count=2, target_mode="pqc")
    st = await _run(orch)
    assert st.phase == MigrationPhase.ROLLED_BACK
    assert not auth.is_enrolled("CP000")
    assert not auth.is_enrolled("CP001")


@pytest.mark.asyncio
async def test_overlap_safety_failed_dispatch_unenrols():
    caps = {f"CP{i:03d}": ["ECDSA-P256", "ML-DSA-44"] for i in range(6)}
    orch, fleet, auth = _build(caps, fail_ids={"CP005"})
    orch.start_migration(wave_size=5, canary_count=1, target_mode="pqc")
    await _run(orch)
    assert not auth.is_enrolled("CP005")
    assert fleet.get_identity("CP005").migration_state == MigrationState.ROLLED_BACK


@pytest.mark.asyncio
async def test_migration_events_emitted():
    caps = {f"CP{i:03d}": ["ECDSA-P256", "ML-DSA-44"] for i in range(5)}
    events = []
    orch, fleet, auth = _build(caps, events=events)
    orch.start_migration(wave_size=3, canary_count=2, target_mode="pqc")
    await _run(orch)
    for expected in ("migration_started", "wave_started",
                     "wave_completed", "migration_completed"):
        assert expected in events


@pytest.mark.asyncio
async def test_status_starts_idle():
    caps = {"CP001": ["ML-DSA-44"]}
    orch, _, _ = _build(caps)
    st = orch.get_migration_status()
    assert st.phase == MigrationPhase.IDLE


@pytest.mark.asyncio
async def test_double_start_raises():
    caps = {f"CP{i:03d}": ["ML-DSA-44"] for i in range(20)}
    orch, _, _ = _build(caps)
    orch.start_migration(wave_size=2, canary_count=1, target_mode="pqc")
    with pytest.raises(RuntimeError):
        orch.start_migration(wave_size=2, canary_count=1, target_mode="pqc")
    await _run(orch)