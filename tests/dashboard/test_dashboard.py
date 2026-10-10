"""
C-F6 — the live dashboard's server side: the mock fleet and dashboard.serve.
Track C (tests).

One happy-path test per feature (FINAL 2-DAY PLAN rule), plus the two
safety checks that matter for a demo: the static server cannot be walked
out of dashboard/static/, and an unreachable CSMS is reported, not crashed.

The page itself (dashboard/static/app.js) is plain JavaScript with no build
step; it is checked by opening it against `python -m dashboard.serve --mock`
(see the PR description), not by pytest.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from dashboard import serve
from dashboard.mock import ALGORITHM, MAX_POWER_W, PI_ID, STEP_S, MockFleet


class Clock:
    def __init__(self) -> None:
        self.t = 1_790_000_000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def run_to_end(fleet: MockFleet, clock: Clock, limit_s: float = 120.0) -> dict:
    waited = 0.0
    while waited < limit_s:
        clock.advance(STEP_S)
        waited += STEP_S
        status = fleet.migration()
        if status["phase"] in ("completed", "rolled_back", "failed"):
            return status
    raise AssertionError("migration did not finish")


def pq_lines(fleet: MockFleet, trigger: str | None = None) -> list[dict]:
    out = [e for e in fleet.events_after(0, 2000)["events"]
           if e["event_type"] == "connection_attempt" and e["payload"].get("transition") == "pq_auth"]
    return [e for e in out if trigger is None or e["payload"]["trigger"] == trigger]


# -- the mock fleet -----------------------------------------------------------------


def test_the_mock_starts_with_a_charging_fleet_and_the_pi():
    fleet = MockFleet(clock=Clock())
    snap = fleet.fleet()
    assert snap["total_stations"] == 50 and snap["connected_count"] == 50
    assert snap["aggregate_power_w"] == pytest.approx(50 * MAX_POWER_W)
    pi = next(s for s in snap["stations"] if s["station_id"] == PI_ID)
    assert pi["power_source"] == "scaled" and pi["measured_mw"] > 0
    assert fleet.health()["mock"] is True                  # the page shows MOCK


def test_a_migration_runs_canary_then_waves_and_skips_legacy_chargers():
    clock = Clock()
    fleet = MockFleet(clock=clock)
    code, body = fleet.start(wave_size=10, canary_count=5)
    assert code == 200 and body["migration_id"]
    status = run_to_end(fleet, clock)
    assert status["phase"] == "completed"
    assert (status["migrated"], status["incompatible"], status["pending"]) == (45, 5, 0)
    assert status["waves"][0]["is_canary"] and len(status["waves"][0]["station_ids"]) == 5
    enrolled = [e for e in fleet.events_after(0, 2000)["events"]
                if e["payload"].get("transition") == "pq_enrolled"]
    checks = pq_lines(fleet, "migration")
    assert len(enrolled) == len(checks) == 45
    # The key checked is the key the charger enrolled.
    assert {e["payload"]["key_id"] for e in enrolled} == {c["payload"]["key_id"] for c in checks}
    assert all(c["payload"]["algorithm"] == ALGORITHM for c in checks)


def test_pqc_is_refused_as_not_built():
    fleet = MockFleet(clock=Clock())
    code, body = fleet.start(target_mode="pqc")
    assert code == 400 and "L17" in body["error"]


def test_refusers_mid_fleet_roll_the_wave_back_and_halt():
    clock = Clock()
    fleet = MockFleet(clock=clock, halt=True)
    fleet.start(wave_size=10, canary_count=5)
    status = run_to_end(fleet, clock)
    assert status["phase"] == "rolled_back"
    rolled = [w for w in status["waves"] if w["phase"] == "rolled_back"]
    assert len(rolled) == 1 and {"CP0021", "CP0025"} <= set(rolled[0]["station_ids"])
    assert any(w["phase"] == "queued" for w in status["waves"])  # later waves never ran: halt
    assert status["pending"] > 0


def test_a_rotation_gives_every_migrated_charger_a_new_key_without_disconnecting():
    clock = Clock()
    fleet = MockFleet(n=9, clock=clock)
    fleet.start(wave_size=3, canary_count=2)
    run_to_end(fleet, clock)
    before = {s["station_id"]: s["pq_key_id"] for s in fleet.fleet()["stations"] if s["pq_key_id"]}
    code, _ = fleet.rotate(wave_size=3, canary_count=2)
    assert code == 200
    status = run_to_end(fleet, clock)
    assert status["kind"] == "rotation" and status["phase"] == "completed"
    done = [e for e in fleet.events_after(0, 2000)["events"]
            if e["event_type"] == "rotation_completed" and e["payload"].get("station")]
    assert len(done) == len(before)
    assert all(e["payload"]["old_key_id"] != e["payload"]["new_key_id"] for e in done)
    assert all(s["connection_state"] == "connected" for s in fleet.fleet()["stations"])


def test_a_power_limit_dims_the_pi_and_the_fleet():
    fleet = MockFleet(n=4, clock=Clock())
    fleet.set_limit(MAX_POWER_W / 2)
    snap = fleet.fleet()
    pi = next(s for s in snap["stations"] if s["station_id"] == PI_ID)
    assert snap["aggregate_power_w"] == pytest.approx(5 * MAX_POWER_W / 2)
    assert pi["measured_mw"] == pytest.approx(12.5)
    fleet.set_limit(None)
    assert fleet.fleet()["aggregate_power_w"] == pytest.approx(5 * MAX_POWER_W)


def test_a_storm_makes_every_enrolled_charger_prove_its_key_at_boot():
    clock = Clock()
    fleet = MockFleet(n=9, clock=clock)
    fleet.start(wave_size=3, canary_count=2)
    run_to_end(fleet, clock)
    fleet.storm(outage_s=1.0)
    assert fleet.fleet()["connected_count"] == 0
    clock.advance(10.0)
    snap = fleet.fleet()
    assert snap["connected_count"] == 10
    migrated = [s for s in snap["stations"] if s["migration_state"] == "migrated"]
    assert migrated and all(s["pq_verified"] for s in migrated)
    assert len(pq_lines(fleet, "boot")) == len(migrated)


def test_an_impostor_is_cut_off_with_1008():
    clock = Clock()
    fleet = MockFleet(n=9, clock=clock)
    fleet.start(wave_size=3, canary_count=2)
    run_to_end(fleet, clock)
    assert fleet.impostor("CP0003")[0] == 200
    clock.advance(0.1)
    fleet.fleet()
    closed = [e for e in fleet.events_after(0, 2000)["events"]
              if e["event_type"] == "connection_closed" and e["payload"].get("reason") == "pq_auth_failed"]
    assert closed and closed[0]["station_id"] == "CP0003" and closed[0]["payload"]["code"] == 1008
    assert [c["payload"]["result"] for c in pq_lines(fleet, "boot")] == ["rejected"]


def test_events_are_paged_by_seq():
    fleet = MockFleet(n=3, clock=Clock())
    first = fleet.events_after(0, 2)
    assert [e["seq"] for e in first["events"]] == [1, 2] and first["last_seq"] >= 2
    rest = fleet.events_after(2, 100)
    assert rest["events"][0]["seq"] == 3


# -- dashboard.serve ----------------------------------------------------------------


def test_the_page_and_its_files_are_served():
    for name, kind in (("index.html", "text/html"), ("app.js", "javascript"),
                       ("style.css", "text/css"), ("vendor/echarts.min.js", "javascript")):
        body, ctype = serve.static_file("/dashboard/" + name)
        assert body and kind in ctype
    assert serve.static_file("/dashboard/")[1].startswith("text/html")


def test_the_static_server_cannot_leave_its_folder():
    assert serve.static_file("/dashboard/../serve.py") is None
    assert serve.static_file("/dashboard/../../requirements.txt") is None
    assert serve.static_file("/dashboard/nope.js") is None


def test_the_mock_answers_every_endpoint_the_page_uses():
    fleet = MockFleet(n=3, clock=Clock())
    for path, query in (("/api/health", {}), ("/api/fleet", {}), ("/api/migration", {}),
                        ("/api/events", {"after": ["0"]}), ("/api/fleet/CP0001", {}),
                        ("/api/fleet/limit", {"watts": ["3700"]}), ("/api/fleet/clear-limit", {}),
                        ("/api/migration/start", {"wave_size": ["2"], "canary_count": ["1"],
                                                  "target_mode": ["hybrid"]})):
        status, _ = serve.mock_api(fleet, path, query)
        assert status == 200, path
    assert serve.mock_api(fleet, "/api/nothing", {})[0] == 404


class Upstream(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api/fleet"):
            body = json.dumps({"stations": [], "path": self.path}).encode()
            self.send_response(200)
        else:
            body = json.dumps({"error": "unknown station"}).encode()
            self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _serve(handler_cls) -> tuple[ThreadingHTTPServer, str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _get(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_the_pass_through_forwards_to_the_csms_unchanged():
    upstream, up_url = _serve(Upstream)
    dash, dash_url = _serve(serve.make_handler(mock=None, api=up_url))
    try:
        status, body = _get(dash_url + "/api/fleet?x=1")
        assert status == 200 and body["path"] == "/api/fleet?x=1"
        assert _get(dash_url + "/api/migration")[0] == 404        # the CSMS's own error, passed on
    finally:
        dash.shutdown()
        upstream.shutdown()


def test_an_unreachable_csms_is_a_502_not_a_crash():
    dash, dash_url = _serve(serve.make_handler(mock=None, api="http://127.0.0.1:9", timeout_s=1.0))
    try:
        status, body = _get(dash_url + "/api/fleet")
        assert status == 502 and body["error"] == "CSMS not reachable"
    finally:
        dash.shutdown()
