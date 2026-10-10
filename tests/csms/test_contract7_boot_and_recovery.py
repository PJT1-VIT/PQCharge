"""
Contract 7, Track A part 1: the boot hook (A-P1), pq_verified and the
per-mode recovery rule (A-P2), and the migration emitter's L31/L34 duties.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket

import pytest
from websockets.asyncio.server import serve

from csms.events import EventLog
from csms.fleet import StationView
from csms.handlers import CSMSHandlers
from csms.migration import orchestrator_emitter
from csms.registry import SessionRegistry


# =====================================================================
# A-P2: the recovery rule, from the row alone
# =====================================================================

def _view(**fields):
    base = {"station_id": "CP0001", "connection_state": "connected",
            "boot_accepted": True}
    base.update(fields)
    return StationView(**base)


@pytest.mark.parametrize("fields, recovered", [
    # classical, or not enrolled: unchanged (connected AND booted)
    ({}, True),
    ({"boot_accepted": False}, False),
    ({"connection_state": "disconnected"}, False),
    ({"pq_verified": False, "pq_check_required": False}, True),
    # hybrid AND enrolled: also needs the key check on this connection
    ({"pq_check_required": True}, False),
    ({"pq_check_required": True, "pq_verified": True}, True),
    ({"pq_check_required": True, "pq_verified": True, "boot_accepted": False}, False),
])
def test_is_recovered_per_mode(fields, recovered):
    assert _view(**fields).is_recovered is recovered


def test_track_c_watcher_applies_the_same_rule_from_json():
    """Track C's FleetWatcher rebuilds StationView from /api/fleet JSON
    (harness/load_generator.py). It must count exactly what is_recovered
    counts, including the hybrid case, without any change on its side."""
    from harness.load_generator import FleetWatcher

    rows = [
        _view(station_id="A"),                                         # classical
        _view(station_id="B", pq_check_required=True),                 # waiting for check
        _view(station_id="C", pq_check_required=True, pq_verified=True),
        _view(station_id="D", boot_accepted=False),
    ]
    snapshot = json.loads(json.dumps({"stations": [v.to_dict() for v in rows]}))
    assert FleetWatcher._recovered(snapshot) == sum(v.is_recovered for v in rows) == 2


def test_rows_from_an_older_server_still_recover_as_before():
    # A row without the two new fields (an old log, an old server) gets
    # the defaults, i.e. the classical rule.
    from harness.load_generator import FleetWatcher

    old_row = {"station_id": "A", "connection_state": "connected", "boot_accepted": True}
    assert FleetWatcher._recovered({"stations": [old_row]}) == 1


# =====================================================================
# A-P2: pq_verified and pq_check_required in the registry
# =====================================================================

def _registry(mode="classical", enrolled=()):
    registry = SessionRegistry(crypto_mode=mode)
    if enrolled is not None:
        registry.set_enrolment_lookup(lambda sid: sid in set(enrolled))
    return registry


def test_pq_verified_is_per_connection():
    registry = _registry()
    first = registry.register("CP0001", object())
    assert registry.mark_pq_verified("CP0001") is True
    assert registry.get_station("CP0001").pq_verified is True

    registry.register("CP0001", object())          # reconnect
    assert registry.get_station("CP0001").pq_verified is False
    assert first.pq_verified is True               # the old session kept its own flag


def test_a_stale_check_cannot_mark_the_next_connection():
    registry = _registry()
    old = registry.register("CP0001", object())
    registry.register("CP0001", object())          # reconnected during the check
    assert registry.mark_pq_verified("CP0001", session=old) is False
    assert registry.get_station("CP0001").pq_verified is False


def test_mark_pq_verified_on_an_offline_station_does_nothing():
    assert _registry().mark_pq_verified("CP0009") is False


def test_check_required_only_in_hybrid_and_only_when_enrolled():
    hybrid = _registry("hybrid", enrolled={"CP0001"})
    hybrid.register("CP0001", object())
    hybrid.register("CP0002", object())
    hybrid.mark_boot_accepted("CP0001")
    hybrid.mark_boot_accepted("CP0002")
    assert hybrid.get_station("CP0001").pq_check_required is True
    assert hybrid.get_station("CP0002").pq_check_required is False
    assert hybrid.get_station("CP0001").is_recovered is False     # waits for the check
    assert hybrid.get_station("CP0002").is_recovered is True      # not enrolled: classical
    assert hybrid.snapshot().booted_count == 1

    hybrid.mark_pq_verified("CP0001")
    assert hybrid.get_station("CP0001").is_recovered is True
    assert hybrid.snapshot().booted_count == 2

    classical = _registry("classical", enrolled={"CP0001"})
    classical.register("CP0001", object())
    assert classical.get_station("CP0001").pq_check_required is False


def test_no_lookup_means_nobody_is_enrolled():
    registry = _registry("hybrid", enrolled=None)
    registry.register("CP0001", object())
    assert registry.get_station("CP0001").pq_check_required is False


def test_a_failing_lookup_never_breaks_the_fleet_view(caplog):
    registry = SessionRegistry(crypto_mode="hybrid")

    def broken(_sid):
        raise RuntimeError("database gone")

    registry.set_enrolment_lookup(broken)
    registry.register("CP0001", object())
    with caplog.at_level(logging.ERROR):
        assert registry.get_station("CP0001").pq_check_required is False
    assert "enrolment lookup failed" in caplog.text


def test_new_fields_are_in_the_api_row():
    registry = _registry("hybrid", enrolled={"CP0001"})
    registry.register("CP0001", object())
    row = registry.snapshot().to_dict()["stations"][0]
    assert row["pq_verified"] is False and row["pq_check_required"] is True


# =====================================================================
# A-P1: the boot hook
# =====================================================================

def test_listeners_run_on_their_own_tasks_and_errors_are_contained(caplog):
    async def scenario():
        registry = _registry()
        conn = object()
        registry.register("CP0001", conn)
        registry.mark_boot_accepted("CP0001")
        seen, here = [], asyncio.current_task()

        def broken(sid):
            raise RuntimeError("listener bug")

        async def good(sid):
            await asyncio.sleep(0)
            seen.append((sid, asyncio.current_task() is not here))

        registry.add_boot_listener(broken)
        registry.add_boot_listener(good)
        tasks = registry.notify_boot("CP0001", conn)
        await asyncio.gather(*tasks)
        return seen, tasks

    with caplog.at_level(logging.ERROR):
        seen, tasks = asyncio.run(scenario())
    assert seen == [("CP0001", True)]          # ran, on a different task
    assert len(tasks) == 2
    assert "boot listener" in caplog.text and "listener bug" in caplog.text


def test_no_announcement_for_a_replaced_connection_or_unaccepted_boot():
    async def scenario():
        registry = _registry()
        calls = []
        registry.add_boot_listener(calls.append)

        old = registry.register("CP0001", object())
        registry.mark_boot_accepted("CP0001")
        registry.register("CP0001", object())                    # replaced
        stale = registry.notify_boot("CP0001", old.connection)

        current = registry.get_session("CP0001").connection      # not booted yet
        unbooted = registry.notify_boot("CP0001", current)

        offline = registry.notify_boot("CP0009", object())
        await asyncio.sleep(0)
        return stale, unbooted, offline, calls

    stale, unbooted, offline, calls = asyncio.run(scenario())
    assert stale == unbooted == offline == [] and calls == []


class _RecordingConnection:
    """Enough of a websockets connection for the ocpp ChargePoint."""

    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(message)

    async def recv(self):  # pragma: no cover - never reached in this test
        await asyncio.sleep(3600)


def test_listener_runs_only_after_the_boot_reply_was_sent(tmp_path):
    """Contract 7 section 7.5 (1): 'after a BootNotification is accepted
    AND ANSWERED'. Drives the real CSMSHandlers with a real OCPP frame."""

    async def scenario():
        registry = _registry()
        log = EventLog(tmp_path / "events.jsonl")
        conn = _RecordingConnection()
        cp = CSMSHandlers("CP0001", conn, registry=registry, event_log=log)
        registry.register("CP0001", cp)
        replies_when_called = []

        def listener(sid):
            replies_when_called.append(list(conn.sent))

        registry.add_boot_listener(listener)
        frame = json.dumps([2, "boot-1", "BootNotification", {
            "chargingStation": {"model": "M", "vendorName": "V"},
            "reason": "PowerUp",
        }])
        await cp.route_message(frame)
        for _ in range(20):
            if replies_when_called:
                break
            await asyncio.sleep(0)
        log.close()
        return replies_when_called

    (sent_before_listener,) = asyncio.run(scenario())
    assert len(sent_before_listener) == 1
    reply = json.loads(sent_before_listener[0])
    assert reply[0] == 3 and reply[1] == "boot-1"           # CALLRESULT to our boot
    assert reply[2]["status"] == "Accepted"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_live_listener_can_command_the_station_that_just_booted(tmp_path):
    """Real server code + Track A's fake station over a real WebSocket: a
    boot listener sends a command to the station that just booted. Dispatch
    rule 1 would refuse it (DispatchError) if the listener ran on the
    station's receive loop; it must instead get the station's answer."""
    from csms.server import CSMS, SUBPROTOCOLS
    from tests.fixtures.fake_station import run_station

    port = _free_port()

    async def scenario():
        csms = CSMS(host="127.0.0.1", port=port, log_path=str(tmp_path / "e.jsonl"),
                    db_path=None, migration_mode="off", ws_ping_interval=None)
        results = []

        async def listener(sid):
            results.append(await csms.dispatcher.clear_charging_profile(sid))

        csms.registry.add_boot_listener(listener)
        async with serve(csms.on_connect, "127.0.0.1", port,
                         subprotocols=SUBPROTOCOLS,
                         process_request=csms.process_request):
            await run_station("CP0001", f"ws://127.0.0.1:{port}", "TAG-0001",
                              charge_for_s=1.0, meter_every_s=0.5)
        csms.log.close()
        return results

    (result,) = asyncio.run(asyncio.wait_for(scenario(), timeout=30))
    assert result.station_id == "CP0001"
    assert result.ok, result                 # rule 4: Unknown from Clear = success
    assert result.error is None


# =====================================================================
# Migration emitter: L31 (source) and L34 (verified by a migration check)
# =====================================================================

class _FakeLog:
    def __init__(self):
        self.events = []

    def emit(self, event_type, station_id=None, **payload):
        self.events.append((event_type, station_id, payload))
        return payload


def test_passed_migration_check_marks_the_station_verified():
    marked = []
    emit = orchestrator_emitter(_FakeLog(), on_pq_auth_success=marked.append)
    emit("connection_attempt", transition="pq_auth", station="CP0001",
         result="success", trigger="migration")
    emit("connection_attempt", transition="pq_auth", station="CP0002",
         result="rejected", trigger="migration")
    emit("certificate_installed", transition="pq_enrolled", station="CP0003")
    assert marked == ["CP0001"]


def test_a_failing_mark_never_loses_the_event(caplog):
    log = _FakeLog()

    def broken(_sid):
        raise RuntimeError("registry gone")

    emit = orchestrator_emitter(log, on_pq_auth_success=broken)
    with caplog.at_level(logging.ERROR):
        emit("connection_attempt", transition="pq_auth", station="CP0001",
             result="success")
    assert len(log.events) == 1 and "registry gone" in caplog.text


def test_source_is_added_by_the_emitter_never_by_the_caller(caplog):
    # L31: a caller's own `source` would raise TypeError in EventLog.emit
    # and lose the event; it is dropped with a warning instead.
    log = _FakeLog()
    with caplog.at_level(logging.WARNING):
        orchestrator_emitter(log)("wave_started", wave_id=0, source="caller")
    (_t, _sid, payload), = log.events
    assert payload["source"] == "orchestrator"
    assert "L31" in caplog.text


def test_emitter_source_is_configurable_for_the_boot_verifier(tmp_path):
    # A-P3 will build the boot verifier's emitter as source="boot_verifier".
    log = EventLog(tmp_path / "e.jsonl")
    orchestrator_emitter(log, source="boot_verifier")(
        "connection_attempt", transition="pq_auth", station="CP0001",
        result="success", trigger="boot",
    )
    log.close()
    (line,) = (tmp_path / "e.jsonl").read_text().splitlines()
    event = json.loads(line)
    assert event["payload"]["source"] == "boot_verifier"
    assert event["station_id"] is None and event["outcome"] is None
