"""
C-P3 — the charger reports its ready times (Contract 7 section 7.6). Track C (tests).

What these prove:

  * The station calls on_authenticated after every signed challenge with
    since_connect_ms, sign_ms, key_id, connection and challenge_no -- and
    never with key material.
  * challenge_no restarts at 1 on every connection (the first challenge of a
    connection is the boot key check in hybrid mode).
  * A failing hook never costs the station its answer.
  * The load generator writes station_connected (with pq_key_held /
    pq_key_id), station_booted and station_authenticated lines, and records
    whether each charger STARTED with a saved key.
  * End to end over a real socket: a charger holding a saved key, a server
    that challenges after every accepted boot (as Track B's boot verifier
    will), and Track B's real PQAuthenticator verifying the signature. The
    tester diary then gives E1 its secure-ready time.

Real ML-DSA; skips without quantcrypt, like test_pqc_integration.py.
Ports 9290-9291 (clear of test_load_generator.py's 9270-9284).
"""

from __future__ import annotations

import asyncio
import base64
import json
import time

import pytest

pytest.importorskip("quantcrypt", reason="PQ backend; see requirements.txt")
pq = pytest.importorskip("crypto.pq", reason="Track B crypto/pq.py")
pq_auth = pytest.importorskip("crypto.pq_auth", reason="Track B crypto/pq_auth.py")

from ocpp.routing import on  # noqa: E402
from ocpp.v201 import call_result  # noqa: E402

from agent import pqc_messages as pqcm  # noqa: E402
from agent.client import StationClient  # noqa: E402
from agent.config import AgentConfig  # noqa: E402
from agent.station import ChargingStation  # noqa: E402
from analysis import collect  # noqa: E402
from analysis.match import MatchedRun  # noqa: E402
from analysis.measures import e1_handshake  # noqa: E402
from harness import load_generator as lg  # noqa: E402
from harness import timing_log as tl  # noqa: E402
from harness.timing_log import TimingLog  # noqa: E402
from tests.fixtures import fake_csms  # noqa: E402


def station_with_key(tmp_path, station_id="CP0001", **kw):
    """A station that has enrolled once (its key is saved in tmp_path)."""
    st = ChargingStation(AgentConfig(station_id=station_id, csms_url="ws://localhost:9000",
                                     pq_key_dir=str(tmp_path), **kw))
    client = StationClient(station_id, connection=None, commands=st)
    return st, client


async def send(client, message):
    return await client.on_data_transfer(
        vendor_id=message.vendor_id, message_id=message.message_id, data=message.data
    )


async def enrol(station, client):
    reply = await send(client, pqcm.build_enrolment_request(station.config.station_id))
    assert reply.status == pqcm.STATUS_ACCEPTED
    return pqcm.parse_enrolment_reply(reply.data)


def pretend_connected(station, dial_ms_ago: float = 50.0) -> None:
    """What run_once() does when a connection opens (C-P3 part only)."""
    station.connect_times_ms.append(1.0)
    station._dial_started = time.perf_counter() - dial_ms_ago / 1000.0
    station._challenges_this_connection = 0


# -- the station's hook --------------------------------------------------------


@pytest.mark.asyncio
async def test_each_signed_challenge_reports_its_ready_time(tmp_path):
    station, client = station_with_key(tmp_path)
    _alg, _pub, key_id = await enrol(station, client)
    seen = []
    station.on_authenticated = lambda **kw: seen.append(kw)
    pretend_connected(station, dial_ms_ago=50.0)

    for _ in range(2):
        answer = await send(client, pqcm.build_challenge_message(b"n" * 32, key_id=key_id))
        assert answer.status == pqcm.STATUS_ACCEPTED

    assert [s["challenge_no"] for s in seen] == [1, 2]
    first = seen[0]
    assert set(first) == {"since_connect_ms", "sign_ms", "key_id", "connection", "challenge_no"}
    assert first["key_id"] == key_id and first["connection"] == 1
    assert first["since_connect_ms"] >= 50.0
    assert 0.0 < first["sign_ms"] < first["since_connect_ms"]
    # Only an id travels to the diary, never key material (L04).
    assert all(isinstance(v, (int, float, str)) for v in first.values())


@pytest.mark.asyncio
async def test_the_challenge_count_restarts_on_every_connection(tmp_path):
    station, client = station_with_key(tmp_path)
    await enrol(station, client)
    seen = []
    station.on_authenticated = lambda **kw: seen.append(kw)

    pretend_connected(station)
    await send(client, pqcm.build_challenge_message(b"a" * 32))
    pretend_connected(station)                                # a reconnection
    await send(client, pqcm.build_challenge_message(b"b" * 32))

    assert [(s["connection"], s["challenge_no"]) for s in seen] == [(1, 1), (2, 1)]
    # No key_id asked for -> the current key is named.
    assert seen[1]["key_id"] == station.pq.key_id


@pytest.mark.asyncio
async def test_the_previous_key_is_named_when_the_server_asks_for_it(tmp_path):
    station, client = station_with_key(tmp_path)
    _a, _p, old_id = await enrol(station, client)
    await enrol(station, client)                               # rotation
    seen = []
    station.on_authenticated = lambda **kw: seen.append(kw)
    pretend_connected(station)

    answer = await send(client, pqcm.build_challenge_message(b"c" * 32, key_id=old_id))
    assert answer.status == pqcm.STATUS_ACCEPTED
    assert seen[0]["key_id"] == old_id != station.pq.key_id


@pytest.mark.asyncio
async def test_a_failing_hook_never_costs_the_answer(tmp_path):
    station, client = station_with_key(tmp_path)
    await enrol(station, client)

    def broken(**_kw):
        raise RuntimeError("diary full")

    station.on_authenticated = broken
    pretend_connected(station)
    answer = await send(client, pqcm.build_challenge_message(b"d" * 32))
    assert answer.status == pqcm.STATUS_ACCEPTED


@pytest.mark.asyncio
async def test_a_refused_challenge_is_not_reported(tmp_path):
    station = ChargingStation(AgentConfig(station_id="CP0001", csms_url="ws://localhost:9000"))
    client = StationClient("CP0001", connection=None, commands=station)
    seen = []
    station.on_authenticated = lambda **kw: seen.append(kw)
    pretend_connected(station)
    answer = await send(client, pqcm.build_challenge_message(b"e" * 32))
    assert answer.status == pqcm.STATUS_REJECTED                # holds no key
    assert seen == []


# -- the load generator's lines -------------------------------------------------


def make_runner(tmp_path):
    log = TimingLog(tmp_path / "t.jsonl", run_id="c7run", experiment="unit", n_stations=1)
    runner = lg.FleetRunner(AgentConfig(station_id="CP0001", csms_url="ws://localhost:9"),
                            lg.FleetSpec(n=1), log)
    return runner, log


@pytest.mark.asyncio
async def test_the_load_generator_writes_the_contract7_lines(tmp_path):
    keys = tmp_path / "keys"
    station, client = station_with_key(keys)
    await enrol(station, client)
    runner, log = make_runner(tmp_path)

    runner._connected_hook("CP0001", station)(12.5, 1, 1)
    runner._booted_hook("CP0001")(since_connect_ms=30.0, connection=1)
    runner._authenticated_hook("CP0001")(since_connect_ms=80.0, sign_ms=1.5,
                                         key_id=station.pq.key_id, connection=1, challenge_no=1)
    log.close()

    rows = {r["event_type"]: r for r in TimingLog.read(tmp_path / "t.jsonl")}
    assert rows[tl.STATION_CONNECTED]["pq_key_held"] is True
    assert rows[tl.STATION_CONNECTED]["pq_key_id"] == station.pq.key_id
    assert rows[tl.STATION_BOOTED]["since_connect_ms"] == 30.0
    auth = rows[tl.STATION_AUTHENTICATED]
    assert (auth["connection"], auth["challenge_no"], auth["since_connect_ms"], auth["sign_ms"]) == (1, 1, 80.0, 1.5)
    assert "private_key" not in json.dumps(auth) and "public_key" not in json.dumps(auth)


def test_the_connected_hook_without_a_station_is_unchanged(tmp_path):
    runner, log = make_runner(tmp_path)
    runner._connected_hook("CP0001")(12.5, 1, 1)
    log.close()
    row = TimingLog.read(tmp_path / "t.jsonl")[-1]
    assert "pq_key_held" not in row and row["connect_ms"] == 12.5


def test_the_outcome_records_the_key_at_start_and_end():
    fields = {f for f in lg.StationOutcome.__dataclass_fields__}
    assert {"pq_key_id", "pq_key_held_at_start", "pq_key_id_at_start"} <= fields
    result = lg.FleetResult("e", "r", 2, "hybrid", False, outcomes=[
        lg.StationOutcome("CP0001", pq_key_held_at_start=True),
        lg.StationOutcome("CP0002"),
    ])
    assert result.to_dict()["pq_keys_at_start"] == 1
    assert "PQ keys at start     1/2" in result.describe()


# -- end to end: a server that checks the key after every boot ------------------


class BootCheckingHandlers(fake_csms.FakeCSMSHandlers):
    """
    FakeCSMS + the hybrid boot check (Contract 7 section 7.5): after an
    accepted, ANSWERED boot, challenge the charger on a separate task and
    verify with Track B's real PQAuthenticator.
    """

    auth = None
    results: list = []

    @on("BootNotification")
    async def on_boot_notification(self, charging_station: dict, reason: str, **kwargs):
        reply = await super().on_boot_notification(charging_station, reason, **kwargs)
        asyncio.ensure_future(self._check())
        return reply

    async def _check(self):
        await asyncio.sleep(0.05)               # the boot reply goes out first
        nonce = self.auth.issue_challenge(self.id)
        response = await self.call(pqcm.build_challenge_message(nonce))
        ok = response.status == pqcm.STATUS_ACCEPTED and self.auth.verify_response(
            self.id, pqcm.parse_signature(response.data))
        BootCheckingHandlers.results.append(ok)


@pytest.mark.asyncio
async def test_secure_ready_end_to_end(tmp_path, monkeypatch):
    keys = tmp_path / "keys"
    first, client = station_with_key(keys)
    _alg, public_key, key_id = await enrol(first, client)

    BootCheckingHandlers.auth = pq_auth.PQAuthenticator(pq.PQProvider())
    BootCheckingHandlers.auth.enrol("CP0001", public_key)       # server keeps the public key only
    BootCheckingHandlers.results = []
    monkeypatch.setattr(fake_csms, "FakeCSMSHandlers", BootCheckingHandlers)

    log = TimingLog(tmp_path / "unit_n1_hybrid.jsonl", run_id="e2e", experiment="unit",
                    n_stations=1, crypto_mode="hybrid")
    config = AgentConfig(station_id="CP0001", csms_url="ws://localhost:9290", crypto_mode="hybrid",
                         charge_for_s=0.6, meter_every_s=0.1, log_level="WARNING",
                         reconnect_max_attempts=1, pq_key_dir=str(keys))
    async with fake_csms.FakeCSMS(port=9290):
        result = await lg.FleetRunner(config, lg.FleetSpec(n=1, stagger_s=0.01), log).run()
    log.close()

    assert BootCheckingHandlers.results == [True]
    outcome = result.outcomes[0]
    assert outcome.pq_key_held_at_start and outcome.pq_key_id_at_start == key_id

    rows = TimingLog.read(tmp_path / "unit_n1_hybrid.jsonl")
    booted = [r for r in rows if r["event_type"] == tl.STATION_BOOTED]
    auth = [r for r in rows if r["event_type"] == tl.STATION_AUTHENTICATED]
    assert len(booted) == 1 and len(auth) == 1
    assert auth[0]["challenge_no"] == 1 and auth[0]["key_id"] == key_id
    # dial -> boot accepted -> key check answered, in that order.
    connected = next(r for r in rows if r["event_type"] == tl.STATION_CONNECTED)
    assert connected["connect_ms"] <= booted[0]["since_connect_ms"] < auth[0]["since_connect_ms"]

    # The analysis turns it into E1's hybrid "ready" time.
    harness = collect.read_harness_file(tmp_path / "unit_n1_hybrid.jsonl").runs[0]
    e1 = e1_handshake.measure(MatchedRun(harness=harness))
    assert e1["ready_basis"] == "secure-ready (boot + key check)"
    assert e1["secure_ready_ms"]["n"] == 1
    assert e1["ready_ms"]["median"] == pytest.approx(auth[0]["since_connect_ms"])
    assert e1["boot_ready_ms"]["median"] == pytest.approx(booted[0]["since_connect_ms"])
    assert e1["sign_ms"]["n"] == 1
    # Never key material in the diary.
    text = (tmp_path / "unit_n1_hybrid.jsonl").read_text()
    assert "private_key" not in text
    assert base64.b64encode(public_key).decode()[:40] not in text
