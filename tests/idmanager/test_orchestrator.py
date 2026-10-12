"""
Orchestrator tests — the pytest form of the rotation harness.

Proves the Contract 4 controller end to end with real ML-DSA against a fake
dispatcher and a fake fleet mirroring Contract 6. These are the assertions
behind E3 (migration under load) and the rollback / overlap-safety claims.

B-F1b (2026-10-12): the InstallPQAuth path is gone, so every test here now
runs the Contract 7 flow -- the fake station makes its own key, answers the
enrolment request with the public key, and signs the challenge.
"""

import asyncio

import pytest

from crypto.pq import PQProvider
from crypto.pq_auth import PQAuthenticator, sign_challenge
from crypto.identity import StationIdentity, MigrationState
from idmanager.orchestrator import MigrationOrchestrator
from idmanager.api import MigrationPhase


ALG = "ML-DSA-44"


class _Result:
    def __init__(self, ok, response=None):
        self.ok = ok
        self.outcome = "success" if ok else "rejected"
        self.status = "Accepted" if ok else "Rejected"
        self.response = response
        self.duration_ms = 1.0


class _Stations:
    """
    Fake chargers speaking Contract 7 with real ML-DSA. A request is
    ("enrol", station_id) or ("challenge", nonce, key_id). Stations in
    fail_ids refuse the enrolment; wrong_key ones sign with another key;
    refuse_challenge ones refuse the challenge.
    """

    def __init__(self, provider, *, fail_ids=(), wrong_key=(), refuse_challenge=()):
        self.provider = provider
        self.fail_ids = set(fail_ids)
        self.wrong_key = set(wrong_key)
        self.refuse = set(refuse_challenge)
        self.keys = {}
        self.sent = []        # station ids sent an enrolment request
        self.kinds = []       # (station_id, "enrol" | "challenge")

    async def send(self, station_id, request, *, timeout_s=None):
        kind = request[0]
        self.kinds.append((station_id, kind))
        await asyncio.sleep(0)
        if kind == "enrol":
            self.sent.append(station_id)
            if station_id in self.fail_ids:
                return _Result(ok=False)
            priv, pub = self.provider.generate_keypair()
            self.keys[station_id] = priv
            return _Result(ok=True, response=pub)
        if station_id in self.refuse:
            return _Result(ok=False)
        key = self.keys[station_id]
        if station_id in self.wrong_key:
            key, _ = self.provider.generate_keypair()
        return _Result(ok=True, response=sign_challenge(self.provider, key, request[1]))


def _contract7_options():
    return dict(
        enrolment_request_factory=lambda sid, algorithm: ("enrol", sid),
        public_key_parser=lambda pub: (ALG, pub, pub[:4].hex()),
        challenge_message_factory=lambda nonce, key_id=None: ("challenge", nonce, key_id),
        signature_parser=lambda response: response,
    )


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
        dispatcher=_Stations(provider, fail_ids=fail_ids or ()),
        authenticator=auth,
        fleet=_Adapter(fleet),
        event_emitter=emitter,
        **_contract7_options(),
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

# =====================================================================
# Day 12 -- fixes before the A+B+C session
# =====================================================================



def _counts_sum(st):
    """Contract 4: the per-state counts must add up to total_stations."""
    return (st.pending + st.in_progress + st.migrated
            + st.rolled_back + st.incompatible) == st.total_stations


@pytest.mark.asyncio
async def test_counts_sum_when_every_station_in_a_wave_fails():
    # Track A's report: when every station in a wave fails individually,
    # rolled_back stayed 0 and the counts fell short of total_stations.
    # This is exactly Stage 6's wave 5 (five refusers).
    caps = {f"CP{i:03d}": ["ECDSA-P256", "ML-DSA-44"] for i in range(6)}
    failing = {f"CP{i:03d}" for i in range(1, 6)}
    orch, fleet, auth = _build(caps, fail_ids=failing)
    orch.start_migration(wave_size=5, canary_count=1, target_mode="pqc")
    st = await _run(orch)
    assert st.phase == MigrationPhase.ROLLED_BACK
    assert st.migrated == 1           # the canary
    assert st.rolled_back == 5        # was 0 before the fix
    assert _counts_sum(st)


@pytest.mark.asyncio
async def test_counts_sum_when_a_mixed_wave_rolls_back():
    # 2 of 4 fail (50% > 20%): the 2 failures AND the 2 that had migrated
    # are all rolled back, and nothing is double-counted.
    caps = {f"CP{i:03d}": ["ECDSA-P256", "ML-DSA-44"] for i in range(5)}
    orch, fleet, auth = _build(caps, fail_ids={"CP001", "CP002"})
    orch.start_migration(wave_size=4, canary_count=1, target_mode="pqc")
    st = await _run(orch)
    assert st.phase == MigrationPhase.ROLLED_BACK
    assert (st.migrated, st.rolled_back) == (1, 4)
    assert _counts_sum(st)


class _AdapterWithLiveness(_Adapter):
    def __init__(self, fleet, offline):
        super().__init__(fleet)
        self._offline = set(offline)

    def is_connected(self, sid):
        return sid not in self._offline


def _build_with(caps, *, offline=(), skip_offline=False, fail_ids=None):
    provider = PQProvider()
    fleet = _FakeFleet(caps)
    auth = PQAuthenticator(provider)
    # an offline station cannot answer, so the fake dispatcher fails it
    dispatcher = _Stations(provider, fail_ids=set(fail_ids or ()) | set(offline))
    orch = MigrationOrchestrator(
        dispatcher=dispatcher,
        authenticator=auth,
        fleet=_AdapterWithLiveness(fleet, offline),
        skip_offline=skip_offline,
        **_contract7_options(),
    )
    return orch, fleet, auth, dispatcher


@pytest.mark.asyncio
async def test_offline_canary_station_is_deferred_not_failed():
    # Track A's Day 9 case: one never-connected station in the canary
    # rolled back the whole migration. With skip_offline it is deferred.
    caps = {f"CP{i:03d}": ["ECDSA-P256", "ML-DSA-44"] for i in range(5)}
    orch, fleet, auth, dispatcher = _build_with(caps, offline={"CP000"}, skip_offline=True)
    orch.start_migration(wave_size=2, canary_count=1, target_mode="pqc")
    st = await _run(orch)
    assert st.phase == MigrationPhase.COMPLETED
    assert fleet.get_identity("CP000").migration_state == MigrationState.PENDING
    assert not auth.is_enrolled("CP000")
    assert "CP000" not in dispatcher.sent          # never sent a key
    assert (st.migrated, st.pending) == (4, 1)
    assert _counts_sum(st)


@pytest.mark.asyncio
async def test_offline_station_still_fails_when_skip_offline_is_off():
    # Default behaviour is unchanged: the Day 9 wiring keeps working.
    caps = {f"CP{i:03d}": ["ECDSA-P256", "ML-DSA-44"] for i in range(5)}
    orch, fleet, auth, _ = _build_with(caps, offline={"CP000"}, skip_offline=False)
    orch.start_migration(wave_size=2, canary_count=1, target_mode="pqc")
    st = await _run(orch)
    assert st.phase == MigrationPhase.ROLLED_BACK
    assert _counts_sum(st)


def _build_verifying(caps, **station_kw):
    provider = PQProvider()
    fleet = _FakeFleet(caps)
    auth = PQAuthenticator(provider)
    stations = _Stations(provider, **station_kw)
    events = []
    orch = MigrationOrchestrator(
        dispatcher=stations,
        authenticator=auth,
        fleet=_Adapter(fleet),
        event_emitter=lambda ev, **k: events.append((ev, k)),
        **_contract7_options(),
    )
    return orch, fleet, auth, stations, events


@pytest.mark.asyncio
async def test_station_migrates_only_after_proving_possession():
    caps = {f"CP{i:03d}": ["ECDSA-P256", "ML-DSA-44"] for i in range(4)}
    orch, fleet, auth, stations, events = _build_verifying(caps)
    orch.start_migration(wave_size=3, canary_count=1, target_mode="pqc")
    st = await _run(orch)
    assert st.phase == MigrationPhase.COMPLETED and st.migrated == 4
    # every station got an enrolment request AND a challenge
    assert sorted(k for _, k in stations.kinds) == ["challenge"] * 4 + ["enrol"] * 4
    checks = [k for ev, k in events if ev == "connection_attempt"]
    assert len(checks) == 4
    assert all(k["transition"] == "pq_auth" and k["result"] == "success" for k in checks)


@pytest.mark.asyncio
async def test_wrong_key_signature_fails_the_station():
    caps = {f"CP{i:03d}": ["ECDSA-P256", "ML-DSA-44"] for i in range(6)}
    orch, fleet, auth, _, events = _build_verifying(caps, wrong_key={"CP003"})
    orch.start_migration(wave_size=5, canary_count=1, target_mode="pqc")
    st = await _run(orch)
    # 1 of 5 = 20%, not above the threshold: the wave completes without CP003
    assert st.phase == MigrationPhase.COMPLETED
    assert fleet.get_identity("CP003").migration_state == MigrationState.ROLLED_BACK
    assert not auth.is_enrolled("CP003")
    assert (st.migrated, st.rolled_back) == (5, 1)
    assert _counts_sum(st)
    rejected = [k for ev, k in events
                if ev == "connection_attempt" and k["result"] == "rejected"]
    assert [k["station"] for k in rejected] == ["CP003"]
    assert rejected[0]["detail"] == "signature did not verify"


@pytest.mark.asyncio
async def test_refused_challenge_fails_the_station():
    caps = {f"CP{i:03d}": ["ECDSA-P256", "ML-DSA-44"] for i in range(6)}
    orch, fleet, auth, _, _ = _build_verifying(caps, refuse_challenge={"CP002"})
    orch.start_migration(wave_size=5, canary_count=1, target_mode="pqc")
    await _run(orch)
    assert fleet.get_identity("CP002").migration_state == MigrationState.ROLLED_BACK
    assert not auth.is_enrolled("CP002")


def test_challenge_factory_without_parser_is_rejected():
    provider = PQProvider()
    opts = _contract7_options()
    opts.pop("signature_parser")
    with pytest.raises(ValueError):
        MigrationOrchestrator(
            dispatcher=_Stations(provider),
            authenticator=PQAuthenticator(provider),
            fleet=_Adapter(_FakeFleet({})),
            **opts,
        )


def test_the_enrolment_options_are_now_required():
    """B-F1b: without enrolment_request_factory + public_key_parser there is
    no way to migrate any more (the server-made-key path is gone)."""
    provider = PQProvider()
    opts = _contract7_options()
    opts.pop("enrolment_request_factory")
    opts.pop("public_key_parser")
    with pytest.raises(ValueError, match="required"):
        MigrationOrchestrator(dispatcher=_Stations(provider),
                              authenticator=PQAuthenticator(provider),
                              fleet=_Adapter(_FakeFleet({})), **opts)


def test_the_removed_install_path_keywords_are_refused():
    """L42: A-F3 stopped passing keypair_factory / install_message_factory
    (2026-10-12), so the orchestrator no longer accepts them at all -- the
    server-made-key path cannot come back by accident."""
    provider = PQProvider()
    with pytest.raises(TypeError):
        MigrationOrchestrator(dispatcher=_Stations(provider),
                              authenticator=PQAuthenticator(provider),
                              fleet=_Adapter(_FakeFleet({})),
                              keypair_factory=provider.generate_keypair,
                              install_message_factory=lambda sid, priv: None,
                              **_contract7_options())