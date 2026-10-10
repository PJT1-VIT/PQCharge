"""
Contract 7 section 7.4 (B-P1): enrolment by public key.

The station makes its own ML-DSA key pair; the orchestrator only ever sees
the public key. These tests drive the orchestrator with Track C's REAL
message builders and parsers (agent/pqc_messages.py) and a REAL charger
identity (agent/pq_identity.PQIdentity) per station, with real ML-DSA --
the same pieces the live CSMS wiring (Track A, A-P4) will use:

    enrolment_request_factory = build_enrolment_request
    public_key_parser         = lambda r: parse_enrolment_reply(r.data)
    challenge_message_factory = build_challenge_message
    signature_parser          = lambda r: parse_signature(r.data)

Only the network is faked: _Chargers answers each DataTransfer the way
agent/station.py does (handle_pq_enrolment / handle_pq_challenge), and
returns an object shaped like csms.dispatch.DispatchResult.
"""

import asyncio
import json

import pytest
from ocpp.v201 import call_result

from agent import pqc_messages as pqc
from agent.pq_identity import PQIdentity
from crypto.identity import MigrationState, StationIdentity
from crypto.pq import PQProvider
from crypto.pq_auth import PQAuthenticator
from idmanager.api import MigrationPhase
from idmanager.orchestrator import MigrationOrchestrator

ALG = "ML-DSA-44"


# -- fleet fakes (same shape as tests/idmanager/test_orchestrator.py) ----


class _Fleet:
    def __init__(self, caps, offline=()):
        self.ids = {
            sid: StationIdentity(station_id=sid, current_algorithm="ECDSA-P256",
                                 supported_algorithms=algs)
            for sid, algs in caps.items()
        }
        self.offline = set(offline)

    def migration_candidate_ids(self):
        return sorted(self.ids)

    def supported_algorithms(self, sid):
        return list(self.ids[sid].supported_algorithms)

    def is_connected(self, sid):
        return sid not in self.offline

    def set_state(self, sid, state, wave):
        self.ids[sid].migration_state = state
        self.ids[sid].migration_wave = wave

    def mark_migrated_algorithm(self, sid, alg):
        self.ids[sid].current_algorithm = alg


# -- the network: chargers that answer like agent/station.py -------------


class _Result:
    """Shaped like csms.dispatch.DispatchResult."""

    def __init__(self, ok, response=None, status=None):
        self.ok = ok
        self.outcome = "success" if ok else "rejected"
        self.status = status or ("Accepted" if ok else "Rejected")
        self.response = response
        self.duration_ms = 1.0


class _Chargers:
    """
    One real PQIdentity per station. Misbehaviours are chosen per station:

      refuse_enrolment   answers RequestPQEnrolment with Rejected
      leak_private_key   adds the private key to its reply (an old/buggy charger)
      bad_key_id         reports a key_id that does not match its public key
      wrong_algorithm    claims a different algorithm in its reply
      refuse_challenge   answers PQAuthChallenge with Rejected
      sign_with_other    signs the challenge with a different key
    """

    def __init__(self, provider, **bad):
        self.provider = provider
        self.bad = {k: set(v) for k, v in bad.items()}
        self.identities = {}
        self.sent = []  # (station_id, message_id, data dict)

    def _is(self, kind, sid):
        return sid in self.bad.get(kind, ())

    async def send(self, station_id, request, *, timeout_s=None):
        await asyncio.sleep(0)
        assert request.vendor_id == pqc.PQC_VENDOR_ID
        data = json.loads(request.data)
        self.sent.append((station_id, request.message_id, data))

        if request.message_id == pqc.MSG_REQUEST_ENROLMENT:
            if self._is("refuse_enrolment", station_id):
                return _Result(False, call_result.DataTransfer(status="Rejected"))
            algorithm = pqc.parse_enrolment_request(request.data)
            ident = self.identities.setdefault(
                station_id, PQIdentity(station_id, provider=self.provider))
            public_key, key_id = ident.enrol(algorithm)
            reply = json.loads(pqc.pack_enrolment_reply(algorithm, public_key))
            if self._is("leak_private_key", station_id):
                reply["private_key"] = "c2VjcmV0"
            if self._is("bad_key_id", station_id):
                reply["key_id"] = "0" * 16
            if self._is("wrong_algorithm", station_id):
                reply["algorithm"] = "ML-DSA-65"
            return _Result(True, call_result.DataTransfer(
                status="Accepted", data=json.dumps(reply)))

        if request.message_id == pqc.MSG_CHALLENGE:
            if self._is("refuse_challenge", station_id):
                return _Result(False, call_result.DataTransfer(status="Rejected"))
            nonce, meta = pqc.parse_challenge_data(request.data)
            if self._is("sign_with_other", station_id):
                other = PQIdentity("other", provider=self.provider)
                other.enrol(ALG)
                signature = other.answer_challenge(nonce)
            else:
                signature = self.identities[station_id].answer_challenge(
                    nonce, pqc.challenge_key_id(meta))
            return _Result(True, call_result.DataTransfer(
                status="Accepted", data=pqc.pack_signature(signature)))

        raise AssertionError(f"unexpected message {request.message_id}")

    def messages(self, message_id):
        return [(sid, d) for sid, m, d in self.sent if m == message_id]


# -- build helpers -------------------------------------------------------


@pytest.fixture(scope="module")
def provider():
    return PQProvider()


def _build(provider, caps, *, offline=(), threshold=0.2, **bad):
    fleet = _Fleet(caps, offline=offline)
    auth = PQAuthenticator(provider)
    chargers = _Chargers(provider, **bad)
    events = []
    orch = MigrationOrchestrator(
        dispatcher=chargers,
        authenticator=auth,
        fleet=fleet,
        event_emitter=lambda ev, **k: events.append((ev, k)),
        failure_threshold=threshold,
        target_algorithm=ALG,
        enrolment_request_factory=pqc.build_enrolment_request,
        public_key_parser=lambda r: pqc.parse_enrolment_reply(r.data),
        challenge_message_factory=pqc.build_challenge_message,
        signature_parser=lambda r: pqc.parse_signature(r.data),
        skip_offline=True,
    )
    return orch, fleet, auth, chargers, events


async def _run(orch):
    while not orch.get_migration_status().is_terminal:
        await asyncio.sleep(0.005)
    return orch.get_migration_status()


def _counts_sum(st):
    return st.migrated + st.rolled_back + st.incompatible + st.pending == st.total_stations


def _of(events, name, **match):
    return [k for ev, k in events if ev == name
            and all(k.get(f) == v for f, v in match.items())]


# -- the happy path ------------------------------------------------------


@pytest.mark.asyncio
async def test_station_makes_its_own_key_and_migrates(provider):
    orch, fleet, auth, chargers, events = _build(
        provider, {"CP01": [ALG], "CP02": [ALG], "CP03": [ALG]})
    orch.start_migration(wave_size=2, canary_count=1, target_mode="pqc")
    st = await _run(orch)

    assert st.phase == MigrationPhase.COMPLETED
    assert st.migrated == 3 and st.rolled_back == 0 and _counts_sum(st)
    for sid in ("CP01", "CP02", "CP03"):
        assert fleet.ids[sid].migration_state == MigrationState.MIGRATED
        assert fleet.ids[sid].current_algorithm == ALG
        # The server enrolled exactly the public key the station made.
        assert auth._enrolled[sid] == chargers.identities[sid].public_key


@pytest.mark.asyncio
async def test_no_private_key_ever_leaves_the_server_side(provider):
    """L01/L04: the server sends no key, and never sees a private key."""
    orch, fleet, auth, chargers, events = _build(provider, {"CP01": [ALG]})
    orch.start_migration(wave_size=1, canary_count=1, target_mode="pqc")
    await _run(orch)

    sent_kinds = {m for _, m, _ in chargers.sent}
    assert sent_kinds == {pqc.MSG_REQUEST_ENROLMENT, pqc.MSG_CHALLENGE}
    assert pqc.MSG_INSTALL not in sent_kinds
    for _, _, data in chargers.sent:
        assert "private_key" not in data
    for _, payload in events:
        assert "private_key" not in payload
    assert chargers.messages(pqc.MSG_REQUEST_ENROLMENT) == [("CP01", {"algorithm": ALG})]


@pytest.mark.asyncio
async def test_challenge_names_the_enrolled_key(provider):
    orch, fleet, auth, chargers, events = _build(provider, {"CP01": [ALG]})
    orch.start_migration(wave_size=1, canary_count=1, target_mode="pqc")
    await _run(orch)

    [(sid, data)] = chargers.messages(pqc.MSG_CHALLENGE)
    assert data["key_id"] == chargers.identities["CP01"].key_id
    assert data["key_id"] == pqc.key_id_for(auth._enrolled["CP01"])


# -- events (Contract 7 section 7.6) -------------------------------------


@pytest.mark.asyncio
async def test_pq_enrolled_and_pq_auth_events_have_the_contract_shape(provider):
    orch, fleet, auth, chargers, events = _build(provider, {"CP01": [ALG]})
    orch.start_migration(wave_size=1, canary_count=1, target_mode="pqc")
    await _run(orch)
    key_id = chargers.identities["CP01"].key_id

    [enrolled] = _of(events, "certificate_installed")
    assert enrolled == {"transition": "pq_enrolled", "station": "CP01",
                        "key_id": key_id, "algorithm": ALG,
                        "trigger": "migration", "wave_id": 0}

    [check] = _of(events, "connection_attempt", transition="pq_auth")
    assert check["station"] == "CP01" and check["result"] == "success"
    assert check["key_id"] == key_id and check["trigger"] == "migration"
    assert check["algorithm"] == ALG and check["wave_id"] == 0
    assert check["detail"] == "signature verified"
    assert check["duration_ms"] == 1.0

    # Order: the key is enrolled before the station is challenged.
    names = [ev for ev, _ in events]
    assert names.index("certificate_installed") < names.index("connection_attempt")


@pytest.mark.asyncio
async def test_orchestrator_never_writes_source_into_a_payload(provider):
    """Track A's emitter adds source="orchestrator"; a second "source"
    keyword would raise TypeError inside EventLog.emit."""
    orch, fleet, auth, chargers, events = _build(
        provider, {"CP01": [ALG], "CP02": [ALG]}, refuse_challenge={"CP02"},
        threshold=1.0)
    orch.start_migration(wave_size=1, canary_count=1, target_mode="pqc")
    await _run(orch)
    assert events and all("source" not in k for _, k in events)


# -- a station that misbehaves fails ALONE, and is left un-enrolled -------


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    "refuse_enrolment", "leak_private_key", "bad_key_id",
    "wrong_algorithm", "refuse_challenge", "sign_with_other",
])
async def test_misbehaving_station_is_rolled_back_and_unenrolled(provider, bad):
    caps = {f"CP{i:02d}": [ALG] for i in range(1, 7)}
    orch, fleet, auth, chargers, events = _build(
        provider, caps, threshold=0.5, **{bad: {"CP04"}})
    orch.start_migration(wave_size=5, canary_count=1, target_mode="pqc")
    st = await _run(orch)

    assert st.phase == MigrationPhase.COMPLETED  # 1 of 5 < 50 %
    assert st.migrated == 5 and st.rolled_back == 1 and _counts_sum(st)
    assert fleet.ids["CP04"].migration_state == MigrationState.ROLLED_BACK
    assert fleet.ids["CP04"].current_algorithm == "ECDSA-P256"
    assert not auth.is_enrolled("CP04")


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["refuse_enrolment", "leak_private_key",
                                 "bad_key_id", "wrong_algorithm"])
async def test_bad_enrolment_reply_enrols_nothing_and_sends_no_challenge(provider, bad):
    orch, fleet, auth, chargers, events = _build(
        provider, {"CP01": [ALG]}, **{bad: {"CP01"}})
    orch.start_migration(wave_size=1, canary_count=1, target_mode="pqc")
    st = await _run(orch)

    assert st.phase == MigrationPhase.ROLLED_BACK  # the canary failed
    assert not auth.is_enrolled("CP01")
    assert chargers.messages(pqc.MSG_CHALLENGE) == []
    assert _of(events, "certificate_installed") == []
    assert _of(events, "connection_attempt") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["refuse_challenge", "sign_with_other"])
async def test_failed_key_check_is_reported_with_key_id(provider, bad):
    orch, fleet, auth, chargers, events = _build(
        provider, {"CP01": [ALG]}, **{bad: {"CP01"}})
    orch.start_migration(wave_size=1, canary_count=1, target_mode="pqc")
    await _run(orch)

    [check] = _of(events, "connection_attempt", transition="pq_auth")
    assert check["result"] == "rejected"
    assert check["key_id"] == chargers.identities["CP01"].key_id
    assert check["trigger"] == "migration"
    assert not auth.is_enrolled("CP01")


# -- unchanged rules: threshold, rollback, gates --------------------------


@pytest.mark.asyncio
async def test_wave_over_threshold_rolls_back_stations_that_did_migrate(provider):
    caps = {f"CP{i:02d}": [ALG] for i in range(1, 6)}
    orch, fleet, auth, chargers, events = _build(
        provider, caps, refuse_challenge={"CP03", "CP04"})
    orch.start_migration(wave_size=4, canary_count=1, target_mode="pqc")
    st = await _run(orch)

    assert st.phase == MigrationPhase.ROLLED_BACK  # 2 of 4 > 20 %
    for sid in ("CP02", "CP03", "CP04", "CP05"):
        assert fleet.ids[sid].migration_state == MigrationState.ROLLED_BACK
        assert not auth.is_enrolled(sid)
    assert auth.is_enrolled("CP01")  # the canary wave stays migrated
    assert st.migrated == 1 and _counts_sum(st)


@pytest.mark.asyncio
async def test_incompatible_and_offline_stations_are_not_asked_to_enrol(provider):
    caps = {"CP01": [ALG], "CP02": ["ECDSA-P256"], "CP03": [ALG]}
    orch, fleet, auth, chargers, events = _build(provider, caps, offline={"CP03"})
    orch.start_migration(wave_size=2, canary_count=1, target_mode="pqc")
    st = await _run(orch)

    asked = [sid for sid, _ in chargers.messages(pqc.MSG_REQUEST_ENROLMENT)]
    assert asked == ["CP01"]
    assert st.migrated == 1 and st.incompatible == 1 and st.pending == 1
    assert _counts_sum(st)


@pytest.mark.asyncio
async def test_manual_rollback_unenrols_a_wave(provider):
    orch, fleet, auth, chargers, events = _build(provider, {"CP01": [ALG], "CP02": [ALG]})
    orch.start_migration(wave_size=1, canary_count=1, target_mode="pqc")
    await _run(orch)
    assert auth.is_enrolled("CP02")

    assert orch.rollback(1) is True
    assert not auth.is_enrolled("CP02")
    assert fleet.ids["CP02"].migration_state == MigrationState.ROLLED_BACK


@pytest.mark.asyncio
async def test_re_migration_enrols_the_new_key(provider):
    """A second run makes a new key on the station; the server must hold
    the new public key and the station must sign with it."""
    orch, fleet, auth, chargers, events = _build(provider, {"CP01": [ALG]})
    orch.start_migration(wave_size=1, canary_count=1, target_mode="pqc")
    await _run(orch)
    first = chargers.identities["CP01"].key_id

    orch.start_migration(wave_size=1, canary_count=1, target_mode="pqc")
    st = await _run(orch)
    second = chargers.identities["CP01"].key_id

    assert st.phase == MigrationPhase.COMPLETED and st.migrated == 1
    assert second != first
    assert chargers.identities["CP01"].previous_key_id == first
    assert pqc.key_id_for(auth._enrolled["CP01"]) == second


# -- constructor rules ---------------------------------------------------


def _kw(provider, **over):
    kw = dict(
        dispatcher=None, authenticator=PQAuthenticator(provider), fleet=_Fleet({}),
        enrolment_request_factory=pqc.build_enrolment_request,
        public_key_parser=lambda r: pqc.parse_enrolment_reply(r.data),
        challenge_message_factory=pqc.build_challenge_message,
        signature_parser=lambda r: pqc.parse_signature(r.data),
    )
    kw.update(over)
    return kw


def test_enrolment_mode_needs_no_keypair_or_install_factory(provider):
    MigrationOrchestrator(**_kw(provider))


@pytest.mark.parametrize("missing", ["enrolment_request_factory", "public_key_parser"])
def test_enrolment_pair_must_be_given_together(provider, missing):
    with pytest.raises(ValueError, match="together"):
        MigrationOrchestrator(**_kw(provider, **{missing: None}))


def test_enrolment_mode_requires_proof_of_possession(provider):
    with pytest.raises(ValueError, match="private half"):
        MigrationOrchestrator(**_kw(provider, challenge_message_factory=None,
                                    signature_parser=None))


def test_legacy_mode_still_requires_keypair_and_install_factories(provider):
    with pytest.raises(ValueError, match="required"):
        MigrationOrchestrator(**_kw(provider, enrolment_request_factory=None,
                                    public_key_parser=None))

# -- B-F1 (L28): "hybrid" is the migration target ---------------------------


@pytest.mark.asyncio
async def test_hybrid_target_migrates_capable_and_skips_legacy(provider):
    orch, fleet, auth, chargers, events = _build(
        provider, {"CP01": [ALG], "CP02": ["ECDSA-P256"], "CP03": [ALG]})
    orch.start_migration(wave_size=2, canary_count=1, target_mode="hybrid")
    st = await _run(orch)
    assert st.phase == MigrationPhase.COMPLETED
    assert st.target_mode == "hybrid"
    assert st.migrated == 2 and st.incompatible == 1 and _counts_sum(st)
    assert [sid for sid, _ in chargers.messages(pqc.MSG_REQUEST_ENROLMENT)] == ["CP01", "CP03"]
