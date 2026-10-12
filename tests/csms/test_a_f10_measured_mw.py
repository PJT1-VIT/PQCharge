"""
Track A, A-F10 (L37): the Pi's real INA219 milliwatts reach the dashboard.

Track B (B-F10) sends the reading as OCPP customData on the EXISTING power
sample (agent/gpio_power.py meter_custom_data -> agent/messages.py
meter_value(power_custom_data=...)). The server must:
  - read measured_mw / power_basis from that note, leaving power_w untouched;
  - show them on the station row (measured_mw, power_source), null for every
    simulated charger (Track C's page treats a NUMBER as "this is the Pi");
  - follow the same stale-reading rule as power_w;
  - add them to the transaction event-log line only when the Pi sent them.
"""

from __future__ import annotations

import asyncio
import json
import socket
import urllib.request

import pytest
import websockets
from ocpp.v201 import ChargePoint as CpBase
from ocpp.v201 import call
from websockets.asyncio.server import serve

from agent.messages import meter_value
from csms.fleet import StationView
from csms.metering import HARDWARE_VENDOR_ID, parse_meter_values
from csms.registry import SessionRegistry
from csms.server import CSMS, SUBPROTOCOLS

PI_NOTE = {   # exactly the keys GpioPower.meter_custom_data() sends
    "vendor_id": "pqcharge.hw", "measured_mw": 46.8, "measured_ma": 9.36,
    "measured_v": 5.0, "led_duty": 1.0, "power_basis": "scaled", "sensor": "INA219",
}


# =====================================================================
# parsing
# =====================================================================

def test_the_pis_note_is_read_and_power_is_untouched():
    r = parse_meter_values([meter_value(7400.0, 12.0, power_custom_data=PI_NOTE)])
    assert (r.power_w, r.energy_wh) == (7400.0, 12.0)
    assert (r.measured_mw, r.power_source) == (46.8, "scaled")
    assert r.unrecognised == [] and r.sample_count == 2


def test_a_simulated_charger_has_no_measured_mw():
    r = parse_meter_values([meter_value(7400.0, 12.0)])
    assert r.power_w == 7400.0 and (r.measured_mw, r.power_source) == (None, None)


def test_camel_case_spelling_is_accepted_too():
    mv = meter_value(3700.0, 1.0)
    mv["sampled_value"][0]["custom_data"] = {
        "vendorId": "pqcharge.hw", "measuredMw": 23.4, "powerBasis": "scaled"}
    assert parse_meter_values([mv]).measured_mw == 23.4


@pytest.mark.parametrize("note", [
    {"vendor_id": "someone.else", "measured_mw": 5.0},     # other vendor
    {"measured_mw": 5.0},                                  # no vendor
    {"vendor_id": HARDWARE_VENDOR_ID, "measured_mw": "x"}, # not a number
    {"vendor_id": HARDWARE_VENDOR_ID, "measured_mw": True},
    {"vendor_id": HARDWARE_VENDOR_ID, "measured_mw": float("nan")},
    {"vendor_id": HARDWARE_VENDOR_ID},                     # no reading
    "not a dict",
])
def test_a_bad_note_is_ignored_and_power_still_counts(note):
    mv = meter_value(7400.0, 1.0)
    mv["sampled_value"][0]["custom_data"] = note
    r = parse_meter_values([mv])
    assert r.power_w == 7400.0 and r.measured_mw is None and r.power_source is None


def test_a_note_on_the_energy_sample_is_not_read():
    mv = meter_value(7400.0, 1.0)
    mv["sampled_value"][1]["custom_data"] = dict(PI_NOTE)
    assert parse_meter_values([mv]).measured_mw is None


def test_the_latest_power_sample_wins_note_or_not():
    r = parse_meter_values([meter_value(7400.0, 1.0, power_custom_data=PI_NOTE),
                            meter_value(7400.0, 2.0)])
    assert r.measured_mw is None          # newest sample had no reading: not shown as current


# =====================================================================
# registry / station row
# =====================================================================

def _row(registry, sid="CP0000"):
    return registry.get_station(sid)


def test_row_fields_and_simulated_chargers_stay_null():
    reg = SessionRegistry()
    reg.register("CP0000", object()); reg.register("CP0001", object())
    reg.record_meter("CP0000", power_w=7400.0, measured_mw=46.8, power_source="scaled")
    reg.record_meter("CP0001", power_w=7400.0)
    assert (_row(reg).measured_mw, _row(reg).power_source) == (46.8, "scaled")
    other = _row(reg, "CP0001")
    assert other.measured_mw is None and other.power_source is None
    assert {"measured_mw", "power_source"} <= set(other.to_dict())
    assert {"measured_mw", "power_source"} <= set(StationView.__dataclass_fields__)


def test_stale_reading_does_not_overwrite_and_limit_lowers_it():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    reg = SessionRegistry()
    reg.register("CP0000", object())
    reg.record_meter("CP0000", power_w=7400.0, reading_at=now, measured_mw=46.8, power_source="scaled")
    assert reg.record_meter("CP0000", power_w=7400.0, reading_at=now - timedelta(seconds=30),
                            measured_mw=1.0, power_source="scaled") is False
    assert _row(reg).measured_mw == 46.8
    reg.record_meter("CP0000", power_w=3700.0, reading_at=now + timedelta(seconds=5),
                     measured_mw=23.4, power_source="scaled")
    assert (_row(reg).power_w, _row(reg).measured_mw) == (3700.0, 23.4)


def test_energy_only_reading_keeps_the_last_mw():
    reg = SessionRegistry()
    reg.register("CP0000", object())
    reg.record_meter("CP0000", power_w=7400.0, measured_mw=46.8, power_source="scaled")
    reg.record_meter("CP0000", energy_wh=5.0)
    assert _row(reg).measured_mw == 46.8


def test_mw_is_not_written_to_the_database():
    """Memory only (decision 1): the SQLite schema and old .db files are unchanged."""
    from csms.persistence import STATE_COLUMNS
    assert "measured_mw" not in STATE_COLUMNS and "power_source" not in STATE_COLUMNS


# =====================================================================
# live: the Pi's message over a real socket, read back from /api
# =====================================================================

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Pi(CpBase):
    pass


def test_live_pi_message_shows_on_api_fleet_and_in_the_event_log(tmp_path):
    port = _free_port()
    log_path = tmp_path / "e.jsonl"

    def tx(event_type, seq, power_w, note, charging_state="Charging", **extra):
        return call.TransactionEvent(
            event_type=event_type, timestamp=meter_value(0, 0)["timestamp"],
            trigger_reason="Authorized" if event_type == "Started" else "MeterValuePeriodic",
            seq_no=seq, transaction_info={"transaction_id": "tx-pi", "charging_state": charging_state},
            evse={"id": 1, "connector_id": 1},
            meter_value=[meter_value(power_w, float(seq), power_custom_data=note)], **extra)

    def fetch(path):
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
            return json.loads(r.read())

    async def scenario():
        csms = CSMS(host="127.0.0.1", port=port, log_path=str(log_path),
                    db_path=None, ws_ping_interval=None, migration_mode="off")
        seen = []
        async with serve(csms.on_connect, "127.0.0.1", port,
                         subprotocols=SUBPROTOCOLS, process_request=csms.process_request):
            async with websockets.connect(f"ws://127.0.0.1:{port}/CP0000",
                                          subprotocols=["ocpp2.0.1"]) as ws:
                pi = _Pi("CP0000", ws)
                reader = asyncio.create_task(pi.start())
                await pi.call(call.BootNotification(
                    charging_station={"model": "PQCharge-Pi", "vendor_name": "PQCharge"},
                    reason="PowerUp"))
                await pi.call(tx("Started", 0, 7400.0, PI_NOTE,
                                 id_token={"id_token": "TAG-0000", "type": "ISO14443"}))
                seen.append(await asyncio.to_thread(fetch, "/api/fleet/CP0000"))
                await pi.call(tx("Updated", 1, 3700.0, dict(PI_NOTE, measured_mw=23.4)))
                seen.append(await asyncio.to_thread(fetch, "/api/fleet/CP0000"))
                # the real Pi attaches the note to Ended too (agent/station.py):
                # LED off, the sensor reads ~0 -- shown as measured, never made up
                await pi.call(tx("Ended", 2, 0.0, dict(PI_NOTE, measured_mw=0.4, led_duty=0.0),
                                 charging_state="Idle"))
                seen.append(await asyncio.to_thread(fetch, "/api/fleet/CP0000"))
                reader.cancel()
        csms.log.close()
        return seen

    first, limited, ended = asyncio.run(asyncio.wait_for(scenario(), timeout=30))
    assert (first["power_w"], first["measured_mw"], first["power_source"]) == (7400.0, 46.8, "scaled")
    assert (limited["power_w"], limited["measured_mw"]) == (3700.0, 23.4)
    assert ended["power_w"] == 0.0 and ended["measured_mw"] == 0.4

    lines = [json.loads(x) for x in log_path.read_text().splitlines()]
    tx_lines = [e for e in lines if e["event_type"].startswith("transaction_")
                and "applied_to_live_state" in (e.get("payload") or {})]
    assert [e["payload"].get("measured_mw") for e in tx_lines] == [46.8, 23.4, 0.4]
    assert {e["payload"]["power_source"] for e in tx_lines} == {"scaled"}


def test_a_simulated_chargers_log_line_is_unchanged(tmp_path):
    """No measured_mw / power_source keys at all on a normal charger's line."""
    from tests.fixtures.fake_station import run_station
    port = _free_port()

    async def scenario():
        csms = CSMS(host="127.0.0.1", port=port, log_path=str(tmp_path / "e.jsonl"),
                    db_path=None, ws_ping_interval=None, migration_mode="off")
        async with serve(csms.on_connect, "127.0.0.1", port,
                         subprotocols=SUBPROTOCOLS, process_request=csms.process_request):
            await run_station("CP0001", f"ws://127.0.0.1:{port}", "TAG-0001",
                              charge_for_s=0.5, meter_every_s=0.25)
            row = await asyncio.to_thread(
                lambda: json.loads(urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/fleet/CP0001", timeout=5).read()))
        csms.log.close()
        return row

    row = asyncio.run(asyncio.wait_for(scenario(), timeout=30))
    assert row["measured_mw"] is None and row["power_source"] is None
    lines = [json.loads(x) for x in (tmp_path / "e.jsonl").read_text().splitlines()]
    tx_lines = [e for e in lines if "applied_to_live_state" in (e.get("payload") or {})]
    assert tx_lines and all("measured_mw" not in e["payload"] for e in tx_lines)
