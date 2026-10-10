"""
B-F2: key rotation with an overlap window (M5, L26).

Real ML-DSA, Track C's real builders and charger identity (PQIdentity keeps
its current and previous key), the real boot verifier. Migrate first, then
rotate, and check that nobody is ever left without a working key.
"""

import asyncio

import pytest

from agent import pqc_messages as pqc
from crypto.pq import PQProvider
from crypto.pq_auth import AuthError, PQAuthenticator
from idmanager.api import MigrationPhase
from idmanager.boot_verifier import BootVerifier
from idmanager.orchestrator import MigrationOrchestrator
from tests.idmanager.test_orchestrator_enrolment import ALG, _Chargers, _Fleet, _run

from crypto.identity import MigrationState


@pytest.fixture(scope="module")
def provider():
    return PQProvider()


def _build(provider, n=3, *, threshold=0.2, **bad):
    caps = {f"CP{i:02d}": [ALG] for i in range(1, n + 1)}
    fleet, auth = _Fleet(caps), PQAuthenticator(provider)
    chargers, events = _Chargers(provider), []
    orch = MigrationOrchestrator(
        dispatcher=chargers, authenticator=auth, fleet=fleet,
        event_emitter=lambda ev, **k: events.append((ev, k)),
        failure_threshold=threshold, target_algorithm=ALG,
        enrolment_request_factory=pqc.build_enrolment_request,
        public_key_parser=lambda r: pqc.parse_enrolment_reply(r.data),
        challenge_message_factory=pqc.build_challenge_message,
        signature_parser=lambda r: pqc.parse_signature(r.data),
        key_id_for=pqc.key_id_for,
    )
    return orch, fleet, auth, chargers, events


def _verifier(auth, chargers):
    return BootVerifier(dispatcher=chargers, authenticator=auth,
                        challenge_message_factory=pqc.build_challenge_message,
                        signature_parser=lambda r: pqc.parse_signature(r.data),
                        key_id_for=pqc.key_id_for)


@pytest.mark.asyncio
async def test_every_station_gets_a_new_key_and_stays_migrated(provider):
    orch, fleet, auth, chargers, events = _build(provider)
    orch.start_migration(wave_size=5, canary_count=1, target_mode="hybrid")
    await _run(orch)
    before = {sid: auth.public_key(sid) for sid in fleet.ids}

    orch.start_rotation(wave_size=5, canary_count=1)
    st = await _run(orch)

    assert st.kind == "rotation" and st.phase == MigrationPhase.COMPLETED
    assert st.migrated == 3 and st.rolled_back == 0
    for sid in fleet.ids:
        assert auth.public_key(sid) != before[sid]                       # new key
        assert pqc.key_id_for(auth.public_key(sid)) == chargers.identities[sid].key_id
        assert fleet.ids[sid].migration_state == MigrationState.MIGRATED
    done = [k for ev, k in events if ev == "rotation_completed"]
    assert len(done) == 3
    assert all(k["old_key_id"] == pqc.key_id_for(before[k["station"]]) for k in done)
    assert all(k["new_key_id"] != k["old_key_id"] for k in done)
    checks = [k for ev, k in events if ev == "connection_attempt" and k.get("trigger") == "rotation"]
    assert len(checks) == 3 and all(k["result"] == "success" for k in checks)
    assert any(ev == "migration_started" and k.get("kind") == "rotation" for ev, k in events)
    assert await _verifier(auth, chargers).verify_on_boot("CP01") is True   # new key works at boot


@pytest.mark.asyncio
async def test_failed_rotation_keeps_the_old_key_and_boot_checks_still_pass(provider):
    orch, fleet, auth, chargers, events = _build(provider, n=6, threshold=0.5)
    orch.start_migration(wave_size=5, canary_count=1, target_mode="hybrid")
    await _run(orch)
    old = auth.public_key("CP04")

    chargers.bad.setdefault("sign_with_other", set()).add("CP04")   # will fail its new-key check
    orch.start_rotation(wave_size=5, canary_count=1)
    st = await _run(orch)

    assert st.phase == MigrationPhase.COMPLETED           # 1 of 5 < 50 %
    assert st.migrated == 5 and st.rolled_back == 1
    assert auth.public_key("CP04") == old                 # kept its old key
    assert auth.staged_key("CP04") is None
    assert fleet.ids["CP04"].migration_state == MigrationState.MIGRATED
    [failed] = [k for ev, k in events if ev == "rotation_failed"]
    assert failed["station"] == "CP04" and failed["old_key_id"] == pqc.key_id_for(old)

    chargers.bad["sign_with_other"].discard("CP04")
    # The charger rotated locally (old key = its "previous"); the server still
    # names the old key_id, the charger signs with its previous key -> passes.
    assert await _verifier(auth, chargers).verify_on_boot("CP04") is True


@pytest.mark.asyncio
async def test_old_key_is_valid_during_the_overlap_window(provider):
    """Mid-rotation (new key staged, not yet proven) the station's boot check
    still passes with its OLD key."""
    auth = PQAuthenticator(provider)
    chargers = _Chargers(provider)
    from agent.pq_identity import PQIdentity
    ident = chargers.identities["CP01"] = PQIdentity("CP01", provider=provider)
    old_pk, _ = ident.enrol(ALG)
    auth.enrol("CP01", old_pk)
    new_pk, _ = ident.enrol(ALG)                       # charger rotated
    auth.stage_key("CP01", new_pk)                     # server: window open

    assert await _verifier(auth, chargers).verify_on_boot("CP01") is True
    assert auth.public_key("CP01") == old_pk           # still the identity


@pytest.mark.asyncio
async def test_a_wave_over_threshold_puts_every_old_key_back(provider):
    orch, fleet, auth, chargers, events = _build(provider, n=6)
    orch.start_migration(wave_size=5, canary_count=1, target_mode="hybrid")
    await _run(orch)
    before = {sid: auth.public_key(sid) for sid in fleet.ids}

    chargers.bad.setdefault("refuse_challenge", set()).update({"CP03", "CP04"})
    orch.start_rotation(wave_size=5, canary_count=1)
    st = await _run(orch)

    assert st.phase == MigrationPhase.ROLLED_BACK         # 2 of 5 > 20 %: halt
    assert auth.public_key("CP01") != before["CP01"]      # canary wave kept its rotation
    for sid in ("CP02", "CP03", "CP04", "CP05", "CP06"):
        assert auth.public_key(sid) == before[sid]        # old keys back
        assert fleet.ids[sid].migration_state == MigrationState.MIGRATED
    chargers.bad["refuse_challenge"].clear()
    assert await _verifier(auth, chargers).verify_on_boot("CP02") is True


@pytest.mark.asyncio
async def test_only_enrolled_stations_are_rotated(provider):
    orch, fleet, auth, chargers, events = _build(provider, n=3)
    orch.start_migration(wave_size=5, canary_count=1, target_mode="hybrid")
    await _run(orch)
    auth.unenrol("CP02")
    orch.start_rotation(wave_size=5, canary_count=1)
    st = await _run(orch)
    assert st.total_stations == 2 and st.migrated == 2


def test_authenticator_stage_commit_discard(provider):
    auth = PQAuthenticator(provider)
    with pytest.raises(AuthError):
        auth.stage_key("CP01", b"x")                       # not enrolled
    auth.enrol("CP01", b"old")
    auth.stage_key("CP01", b"new")
    assert auth.public_key("CP01") == b"old" and auth.staged_key("CP01") == b"new"
    auth.discard_staged("CP01")
    assert auth.staged_key("CP01") is None and auth.public_key("CP01") == b"old"
    auth.stage_key("CP01", b"new")
    assert auth.commit_staged("CP01") == b"old"
    assert auth.public_key("CP01") == b"new" and auth.staged_key("CP01") is None
    auth.stage_key("CP01", b"newer")
    auth.unenrol("CP01")
    assert auth.staged_key("CP01") is None
