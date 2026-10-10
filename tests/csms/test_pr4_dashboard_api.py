"""
Track A PR 4 -- A-F4, the dashboard's data API (Contract 6, additive):
  (1) GET /api/events?after=&limit=  -> {events: [Contract 3 dict + seq], last_seq}
  (2) StationView: tls_cipher, identity_ok, pq_key_id, last_pq_check
  (3) /api/health: tls, identity_check, pq_algorithm
  (4) dashboard/static/ served at /dashboard/
Shapes are checked against Track C's dashboard/mock.py, the page's reader.
"""

from __future__ import annotations

import asyncio
import json
import socket
import urllib.request
from urllib.parse import parse_qs, urlparse

import pytest
from websockets.asyncio.server import serve

from csms.events import RECENT_EVENTS, EventLog
from csms.fleet import StationView
from csms.migration import orchestrator_emitter, record_pq_check
from csms.registry import SessionRegistry
from csms.server import CSMS, SUBPROTOCOLS


def _csms(tmp_path, **kwargs):
    kwargs.setdefault("migration_mode", "off")
    return CSMS(host="127.0.0.1", port=0, log_path=str(tmp_path / "e.jsonl"),
                db_path=None, ws_ping_interval=None, **kwargs)


def _get(csms, path_and_query):
    parsed = urlparse(path_and_query)
    response = csms.process_request(None, type("R", (), {"path": path_and_query})())
    return response


def _json(csms, path_and_query):
    response = _get(csms, path_and_query)
    return response.status_code, json.loads(response.body)


# =====================================================================
# (1) /api/events
# =====================================================================

def test_events_after_returns_the_file_lines_plus_seq(tmp_path):
    log = EventLog(tmp_path / "e.jsonl")
    for i in range(5):
        log.emit("state_changed", f"CP{i:04d}", transition="booted")
    out = log.events_after(after=2, limit=2)
    assert out["last_seq"] == 5
    assert [e["seq"] for e in out["events"]] == [3, 4]
    file_lines = [json.loads(x) for x in (tmp_path / "e.jsonl").read_text().splitlines()]
    assert "seq" not in file_lines[0]                         # file format unchanged
    expected = dict(file_lines[2]); expected["seq"] = 3
    assert out["events"][0] == expected                       # same dict as the file
    log.close()


def test_events_buffer_keeps_only_the_recent_ones(tmp_path):
    log = EventLog(tmp_path / "e.jsonl")
    for _ in range(RECENT_EVENTS + 10):
        log.emit("heartbeat_test")
    out = log.events_after(after=0, limit=10_000)
    assert len(out["events"]) == RECENT_EVENTS
    assert out["events"][0]["seq"] == 11 and out["last_seq"] == RECENT_EVENTS + 10
    log.close()


def test_a_reader_ahead_of_a_restarted_server_resumes(tmp_path):
    log = EventLog(tmp_path / "e.jsonl")
    log.emit("server_started")
    out = log.events_after(after=999)                         # seq from the old run
    assert out == {"events": [], "last_seq": 1}               # -> continue from 1
    log.close()


def test_api_events_route(tmp_path):
    csms = _csms(tmp_path)
    csms.log.emit("state_changed", "CP0001", transition="booted")
    code, body = _json(csms, "/api/events?after=0&limit=10")
    assert code == 200 and body["last_seq"] == 1
    assert body["events"][0]["event_type"] == "state_changed" and body["events"][0]["seq"] == 1
    assert _json(csms, "/api/events")[0] == 200               # defaults
    assert _json(csms, "/api/events?after=x")[0] == 400
    csms.log.close()


# =====================================================================
# (2) station fields
# =====================================================================

def test_station_row_carries_the_inspector_fields():
    registry = SessionRegistry(crypto_mode="hybrid")
    registry.register("CP0001", object(), identity_ok=True,
                      security={"tls_version": "TLSv1.3", "tls_cipher": "TLS_AES_256_GCM_SHA384"})
    registry.set_key_id_lookup(lambda sid: "3f9a00112233aabb" if sid == "CP0001" else None)
    row = registry.get_station("CP0001")
    assert (row.tls_cipher, row.identity_ok, row.pq_key_id, row.last_pq_check) == (
        "TLS_AES_256_GCM_SHA384", True, "3f9a00112233aabb", None)

    registry.record_pq_check("CP0001", result="rejected", trigger="boot", duration_ms=41.0)
    check = registry.get_station("CP0001").last_pq_check
    assert {k: check[k] for k in ("result", "trigger", "duration_ms")} == {
        "result": "rejected", "trigger": "boot", "duration_ms": 41.0}
    assert check["at"]

    registry.register("CP0001", object())                      # reconnect over ws://
    row = registry.get_station("CP0001")
    assert row.tls_cipher is None and row.identity_ok is None
    assert row.last_pq_check["trigger"] == "boot"             # kept: station-level


def test_every_key_check_is_recorded_success_or_failure():
    registry = SessionRegistry()
    registry.register("CP0001", object())
    emit = orchestrator_emitter(_NullLog(), on_pq_auth=record_pq_check(registry))
    emit("connection_attempt", transition="pq_auth", station="CP0001",
         result="rejected", trigger="rotation", duration_ms=12.5)
    assert registry.get_station("CP0001").last_pq_check["result"] == "rejected"
    emit("connection_attempt", transition="pq_auth", station="CP0001",
         result="success", trigger="migration", duration_ms=40.0)
    assert registry.get_station("CP0001").last_pq_check["trigger"] == "migration"


class _NullLog:
    def emit(self, *a, **k):
        return None


def test_a_failing_key_id_lookup_never_breaks_the_fleet_view():
    registry = SessionRegistry()
    registry.register("CP0001", object())

    def broken(_sid):
        raise RuntimeError("no")

    registry.set_key_id_lookup(broken)
    assert registry.get_station("CP0001").pq_key_id is None


# =====================================================================
# Track C's page reads exactly what its mock serves
# =====================================================================

def test_real_rows_and_health_have_every_field_the_mock_has(tmp_path):
    from dashboard.mock import MockFleet

    mock = MockFleet(n=2)
    mock_row = next(iter(mock.fleet()["stations"]))
    real_fields = {f for f in StationView.__dataclass_fields__}
    assert set(mock_row) - real_fields == set(), "page reads a field the server lacks"

    csms = _csms(tmp_path)
    _code, health = _json(csms, "/api/health")
    assert set(mock.health()) - {"mock"} <= set(health)
    assert (health["tls"], health["identity_check"]) == (False, "off")

    csms.log.emit("state_changed", "CP0001", transition="booted")
    real_event = _json(csms, "/api/events?after=0")[1]["events"][0]
    mock_event = mock.events_after(0, 1)["events"][0]
    assert set(mock_event) == set(real_event)
    csms.log.close()


# =====================================================================
# (4) /dashboard/
# =====================================================================

def test_dashboard_files_are_served(tmp_path):
    csms = _csms(tmp_path)
    redirect = _get(csms, "/dashboard")
    assert redirect.status_code == 301 and redirect.headers["Location"] == "/dashboard/"

    index = _get(csms, "/dashboard/")
    assert index.status_code == 200 and index.headers["Content-Type"].startswith("text/html")
    assert b"<html" in index.body.lower()
    js = _get(csms, "/dashboard/app.js")
    assert js.status_code == 200 and "javascript" in js.headers["Content-Type"]
    assert _get(csms, "/dashboard/style.css").headers["Content-Type"].startswith("text/css")
    assert _get(csms, "/dashboard/vendor/echarts.min.js").status_code == 200
    csms.log.close()


@pytest.mark.parametrize("path", [
    "/dashboard/../csms/server.py",
    "/dashboard/%2e%2e/%2e%2e/csms/server.py",
    "/dashboard/..%2F..%2Fcsms%2Fserver.py",
    "/dashboard/__init__.py",
    "/dashboard/nope.html",
])
def test_nothing_outside_the_page_folder_is_served(tmp_path, path):
    csms = _csms(tmp_path)
    assert _get(csms, path).status_code == 404
    csms.log.close()


# =====================================================================
# live: the whole thing over a real socket
# =====================================================================

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_live_page_and_api_over_http(tmp_path):
    from tests.fixtures.fake_station import run_station

    port = _free_port()

    async def scenario():
        csms = CSMS(host="127.0.0.1", port=port, log_path=str(tmp_path / "e.jsonl"),
                    db_path=None, ws_ping_interval=None, migration_mode="off")
        async with serve(csms.on_connect, "127.0.0.1", port,
                         subprotocols=SUBPROTOCOLS, process_request=csms.process_request):
            await run_station("CP0001", f"ws://127.0.0.1:{port}", "TAG-0001",
                              charge_for_s=0.5, meter_every_s=0.25)

            def fetch(path):
                with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
                    return r.headers.get("Content-Type"), r.read()

            page = await asyncio.to_thread(fetch, "/dashboard/")
            events = await asyncio.to_thread(fetch, "/api/events?after=0&limit=500")
            fleet = await asyncio.to_thread(fetch, "/api/fleet")
        csms.log.close()
        return page, events, fleet

    page, events, fleet = asyncio.run(asyncio.wait_for(scenario(), timeout=30))
    assert page[0].startswith("text/html")
    body = json.loads(events[1])
    kinds = [e["event_type"] for e in body["events"]]
    assert "connection_established" in kinds and "transaction_started" in kinds
    assert [e["seq"] for e in body["events"]] == sorted(e["seq"] for e in body["events"])
    row = json.loads(fleet[1])["stations"][0]
    assert {"tls_cipher", "identity_ok", "pq_key_id", "last_pq_check"} <= set(row)
