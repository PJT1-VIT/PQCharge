"""
B-P2: the boot verifier (Contract 7 sections 7.5 (2) and 7.6).

Driven with Track C's REAL message builders/parsers (agent/pqc_messages.py),
a REAL charger identity per station (agent/pq_identity.PQIdentity, which
keeps its current AND previous key) and real ML-DSA-44. The last tests also
use Track A's REAL registry, boot hook and event emitter, wired the way A-P3
will wire them. Only the network is faked: _Chargers answers each
DataTransfer the way agent/station.py does.
"""

import asyncio
import json
import logging

import pytest
from ocpp.v201 import call_result

from agent import pqc_messages as pqc
from agent.pq_identity import PQIdentity
from crypto.pq import PQProvider
from crypto.pq_auth import PQAuthenticator
from idmanager.boot_verifier import BootVerifier
from idmanager.orchestrator import MigrationOrchestrator

ALG = "ML-DSA-44"


class _Result:
    """Shaped like csms.dispatch.DispatchResult."""

    def __init__(self, ok, response=None, outcome=None):
        self.ok = ok
        self.outcome = outcome or ("success" if ok else "rejected")
        self.status = "Accepted" if ok else ("Rejected" if outcome is None else None)
        self.response = response
        self.duration_ms = 2.5


class _Chargers:
    """Real PQIdentity per station; misbehaviour chosen per station."""

    def __init__(self, provider, *, refuse=(), timeout=(), garbage=(),
                 other_key=(), crash=(), delay_s=0.0):
        self.provider = provider
        self.identities = {}
        self.refuse, self.timeout, self.garbage = set(refuse), set(timeout), set(garbage)
        self.other_key, self.crash = set(other_key), set(crash)
        self.delay_s = delay_s
        self.challenges = []  # (station_id, data dict)

    def identity(self, sid):
        return self.identities.setdefault(sid, PQIdentity(sid, provider=self.provider))

    async def send(self, station_id, request, *, timeout_s=None):
        await asyncio.sleep(self.delay_s)
        if station_id in self.crash:
            raise RuntimeError("dispatcher bug")
        data = json.loads(request.data)
        if request.message_id == pqc.MSG_REQUEST_ENROLMENT:
            public_key, _ = self.identity(station_id).enrol(data["algorithm"])
            return _Result(True, call_result.DataTransfer(
                status="Accepted", data=pqc.pack_enrolment_reply(data["algorithm"], public_key)))
        assert request.message_id == pqc.MSG_CHALLENGE
        self.challenges.append((station_id, data))
        if station_id in self.timeout:
            return _Result(False, outcome="timeout")
        if station_id in self.refuse:
            return _Result(False, call_result.DataTransfer(status="Rejected"))
        if station_id in self.garbage:
            return _Result(True, call_result.DataTransfer(status="Accepted", data="{not json"))
        nonce, meta = pqc.parse_challenge_data(request.data)
        if station_id in self.other_key:
            stranger = PQIdentity("x", provider=self.provider)
            stranger.enrol(ALG)
            signature = stranger.answer_challenge(nonce)
        else:
            signature = self.identity(station_id).answer_challenge(
                nonce, pqc.challenge_key_id(meta))
        return _Result(True, call_result.DataTransfer(
            status="Accepted", data=pqc.pack_signature(signature)))


@pytest.fixture(scope="module")
def provider():
    return PQProvider()


def _enrol(auth, chargers, sid):
    """Charger makes its key (Contract 7 7.3); server enrols the public half."""
    public_key, _ = chargers.identity(sid).enrol(ALG)
    auth.enrol(sid, public_key)
    return pqc.key_id_for(public_key)


def _verifier(auth, chargers, events=None, **kw):
    return BootVerifier(
        dispatcher=chargers,
        authenticator=auth,
        challenge_message_factory=pqc.build_challenge_message,
        signature_parser=lambda r: pqc.parse_signature(r.data),
        key_id_for=pqc.key_id_for,
        event_emitter=(lambda ev, **k: events.append((ev, k))) if events is not None else None,
        dispatch_timeout_s=1.0,
        **kw,
    )


# -- not enrolled: classical, no check -----------------------------------


def test_not_enrolled_passes_with_no_check_and_no_event(provider):
    chargers, events = _Chargers(provider), []
    ok = asyncio.run(_verifier(PQAuthenticator(provider), chargers, events).verify_on_boot("CP0001"))
    assert ok is True
    assert chargers.challenges == [] and events == []


# -- enrolled: the check ---------------------------------------------------


def test_enrolled_station_passes_and_the_event_has_the_contract_shape(provider):
    auth, chargers, events = PQAuthenticator(provider), _Chargers(provider), []
    key_id = _enrol(auth, chargers, "CP0001")

    assert asyncio.run(_verifier(auth, chargers, events).verify_on_boot("CP0001")) is True

    [(sid, data)] = chargers.challenges
    assert sid == "CP0001" and data["key_id"] == key_id
    [(name, payload)] = events
    assert name == "connection_attempt"
    assert payload == {"transition": "pq_auth", "station": "CP0001", "result": "success",
                       "detail": "signature verified", "duration_ms": 2.5,
                       "algorithm": ALG, "key_id": key_id, "trigger": "boot"}
    assert "source" not in payload and "wave_id" not in payload   # L31; no wave
    assert "private_key" not in json.dumps(payload)


def test_a_server_that_missed_a_rotation_still_verifies(provider):
    """The charger rotated (new current key) but the server still holds the
    previous one: the challenge names the server's key, the charger signs
    with its previous key, and the check passes (Contract 7 7.3)."""
    auth, chargers = PQAuthenticator(provider), _Chargers(provider)
    old_key_id = _enrol(auth, chargers, "CP0001")
    chargers.identity("CP0001").enrol(ALG)          # charger rotates; server not told
    assert chargers.identity("CP0001").previous_key_id == old_key_id

    assert asyncio.run(_verifier(auth, chargers).verify_on_boot("CP0001")) is True
    assert chargers.challenges[0][1]["key_id"] == old_key_id


@pytest.mark.parametrize("bad, detail", [
    ("refuse", "challenge not answered"),
    ("timeout", "challenge not answered (outcome=timeout"),
    ("garbage", "unreadable signature"),
    ("other_key", "signature did not verify"),
    ("crash", "challenge not sent: RuntimeError"),
])
def test_every_failure_returns_false_and_is_reported(provider, bad, detail):
    auth, chargers, events = PQAuthenticator(provider), _Chargers(provider, **{bad: {"CP0001"}}), []
    key_id = _enrol(auth, chargers, "CP0001")

    assert asyncio.run(_verifier(auth, chargers, events).verify_on_boot("CP0001")) is False

    [(_, payload)] = events
    assert payload["result"] == "rejected" and payload["trigger"] == "boot"
    assert payload["key_id"] == key_id
    assert payload["detail"].startswith(detail)
    assert auth.is_enrolled("CP0001")      # a failed boot check never un-enrols


def test_charger_that_lost_its_key_file_fails(provider):
    """Server enrolled a key the charger no longer has (keys folder deleted,
    --db kept): the charger cannot sign for that key_id -> False (1008)."""
    auth, chargers = PQAuthenticator(provider), _Chargers(provider)
    _enrol(auth, chargers, "CP0001")
    chargers.identities["CP0001"] = PQIdentity("CP0001", provider=provider)  # fresh, no key
    chargers.identity("CP0001").enrol(ALG)                                 # a different key
    chargers.identity("CP0001").enrol(ALG)                                 # and another
    assert asyncio.run(_verifier(auth, chargers).verify_on_boot("CP0001")) is False


def test_an_emitter_fault_never_changes_the_verdict(provider, caplog):
    auth, chargers = PQAuthenticator(provider), _Chargers(provider)
    _enrol(auth, chargers, "CP0001")

    def broken(*a, **k):
        raise RuntimeError("log gone")

    v = _verifier(auth, chargers)
    v._emit = broken
    with caplog.at_level(logging.ERROR):
        assert asyncio.run(v.verify_on_boot("CP0001")) is True
    assert "could not write" in caplog.text


def test_an_internal_fault_fails_closed(provider):
    auth, chargers, events = PQAuthenticator(provider), _Chargers(provider), []
    _enrol(auth, chargers, "CP0001")

    def broken_key_id(_pk):
        raise ValueError("bad key")

    v = BootVerifier(dispatcher=chargers, authenticator=auth,
                     challenge_message_factory=pqc.build_challenge_message,
                     signature_parser=lambda r: pqc.parse_signature(r.data),
                     key_id_for=broken_key_id,
                     event_emitter=lambda ev, **k: events.append((ev, k)))
    assert asyncio.run(v.verify_on_boot("CP0001")) is False
    assert events[0][1]["result"] == "rejected"
    assert events[0][1]["detail"].startswith("internal error")
    assert chargers.challenges == []


def test_many_stations_checked_concurrently(provider):
    auth, chargers = PQAuthenticator(provider), _Chargers(provider, other_key={"CP0003"}, delay_s=0.01)
    for i in range(1, 6):
        _enrol(auth, chargers, f"CP000{i}")
    v = _verifier(auth, chargers)

    async def run():
        return await asyncio.gather(*(v.verify_on_boot(f"CP000{i}") for i in range(1, 6)))

    assert asyncio.run(run()) == [True, True, False, True, True]


# -- L35: a boot check and a migration check for the same station overlap --


def test_overlapping_boot_and_migration_checks_both_pass(provider):
    """Before B-P2 the authenticator kept one challenge per station, so the
    second check replaced the first and BOTH failed. Now each check
    verifies against its own nonce."""
    auth, chargers = PQAuthenticator(provider), _Chargers(provider, delay_s=0.02)
    _enrol(auth, chargers, "CP0001")
    v = _verifier(auth, chargers)

    class _Fleet:
        def migration_candidate_ids(self): return ["CP0001"]
        def supported_algorithms(self, sid): return [ALG]
        def set_state(self, *a): pass
        def mark_migrated_algorithm(self, *a): pass

    orch = MigrationOrchestrator(
        dispatcher=chargers, authenticator=auth, fleet=_Fleet(),
        enrolment_request_factory=pqc.build_enrolment_request,
        public_key_parser=lambda r: pqc.parse_enrolment_reply(r.data),
        challenge_message_factory=pqc.build_challenge_message,
        signature_parser=lambda r: pqc.parse_signature(r.data),
    )

    async def run():
        # Migration check and boot check in flight at the same time.
        migrate = orch._verify_possession("CP0001", key_id=pqc.key_id_for(auth.public_key("CP0001")))
        boot = v.verify_on_boot("CP0001")
        return await asyncio.gather(migrate, boot)

    (migrated_ok, _, _), boot_ok = asyncio.run(run())
    assert migrated_ok is True and boot_ok is True
    assert len(chargers.challenges) == 2


# -- wired the way Track A will wire it (A-P1 hook, A-P2 flag, L31, L34) --


def test_through_track_a_registry_hook_and_emitter(provider, tmp_path):
    from csms.events import EventLog
    from csms.migration import orchestrator_emitter
    from csms.registry import SessionRegistry

    auth, chargers = PQAuthenticator(provider), _Chargers(provider)
    _enrol(auth, chargers, "CP0001")
    _enrol(auth, chargers, "CP0002")
    chargers.other_key.add("CP0002")                  # CP0002 is an impostor

    registry = SessionRegistry(crypto_mode="hybrid")
    registry.set_enrolment_lookup(auth.is_enrolled)
    log = EventLog(tmp_path / "events.jsonl", crypto_mode="hybrid")
    verifier = _verifier(auth, chargers)
    verifier._emit = orchestrator_emitter(log, source="boot_verifier",
                                          on_pq_auth_success=registry.mark_pq_verified)
    verdicts = {}

    async def listener(sid):
        verdicts[sid] = await verifier.verify_on_boot(sid)

    registry.add_boot_listener(listener)

    async def run():
        for sid in ("CP0001", "CP0002", "CP0003"):     # CP0003 not enrolled
            conn = object()
            registry.register(sid, conn)
            registry.mark_boot_accepted(sid)
            await asyncio.gather(*registry.notify_boot(sid, conn))

    asyncio.run(run())
    log.close()

    assert verdicts == {"CP0001": True, "CP0002": False, "CP0003": True}
    assert registry.get_station("CP0001").is_recovered is True     # checked
    assert registry.get_station("CP0002").is_recovered is False    # A-P3 closes it
    assert registry.get_station("CP0003").is_recovered is True     # classical

    lines = [json.loads(l) for l in (tmp_path / "events.jsonl").read_text().splitlines()]
    pq = [e for e in lines if e["payload"].get("transition") == "pq_auth"]
    assert [(e["payload"]["station"], e["payload"]["result"]) for e in pq] == [
        ("CP0001", "success"), ("CP0002", "rejected")]
    for e in pq:
        assert e["payload"]["source"] == "boot_verifier"
        assert e["payload"]["trigger"] == "boot"
        assert e["station_id"] is None and e["outcome"] is None    # §7.6