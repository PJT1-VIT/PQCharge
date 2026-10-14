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

from pathlib import Path

from dashboard import serve
from dashboard.mock import ALGORITHM, MAX_POWER_W, PI_ID, STEP_S, MockFleet

ROOT = Path(__file__).resolve().parents[2]


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
    # The demo fleet (fleet_profile_demo.json): Pi + CP0001-45 capable, CP0046-49 legacy.
    assert (status["migrated"], status["incompatible"], status["pending"]) == (46, 4, 0)
    assert status["waves"][0]["is_canary"] and len(status["waves"][0]["station_ids"]) == 5
    assert status["waves"][0]["station_ids"][0] == PI_ID == "CP0000"   # the Pi is in the canary
    enrolled = [e for e in fleet.events_after(0, 2000)["events"]
                if e["payload"].get("transition") == "pq_enrolled"]
    checks = pq_lines(fleet, "migration")
    assert len(enrolled) == len(checks) == 46
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
    # Track B's expected L09 numbers (fleet_profile_halt_n50.json).
    assert (status["migrated"], status["rolled_back"], status["pending"]) == (15, 10, 25)


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
    # Announced exactly like Track B's B-F2: migration_started with kind "rotation".
    starts = [e for e in fleet.events_after(0, 2000)["events"] if e["event_type"] == "migration_started"]
    assert [e["payload"].get("kind") for e in starts] == [None, "rotation"]


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
    # As the real registry writes it (Track A PR 3): outcome rejected, reason in the payload.
    assert closed and closed[0]["station_id"] == "CP0003" and closed[0]["outcome"] == "rejected"
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


# -- 6c: the 500-charger demo -------------------------------------------------------


def test_the_mock_of_499_follows_the_500_charger_profile():
    """The mock's legacy chargers are exactly the profile's (Track A's own parser)."""
    from csms.migration import load_fleet_profile

    profile = load_fleet_profile(str(ROOT / "tests" / "fixtures" / "fleet_profile_demo_n500.json"))
    assert len(profile) == 500 and profile[PI_ID] == ["ECDSA-P256", ALGORITHM]
    profile_legacy = {sid for sid, algs in profile.items() if ALGORITHM not in algs}
    fleet = MockFleet(n=499, clock=Clock())
    assert fleet.legacy == profile_legacy == {f"CP{i:04d}" for i in range(450, 500)}
    assert set(fleet.stations) == set(profile)                    # CP0000-CP0499, nothing else


def test_a_500_charger_migration_runs_in_waves_of_50():
    clock = Clock()
    fleet = MockFleet(n=499, clock=clock)
    fleet.start(wave_size=50, canary_count=10)
    status = run_to_end(fleet, clock, limit_s=60.0)                # mock time
    assert status["phase"] == "completed"
    assert (status["migrated"], status["incompatible"], status["pending"]) == (450, 50, 0)
    assert len(status["waves"][0]["station_ids"]) == 10 and status["waves"][0]["station_ids"][0] == PI_ID
    assert {len(w["station_ids"]) for w in status["waves"][1:-1]} == {50}


def test_the_page_has_the_large_fleet_mode():
    """The dense grid (6c) is wired: the hint the page rewrites, the switch, the styles."""
    index, _ = serve.static_file("/dashboard/index.html")
    app, _ = serve.static_file("/dashboard/app.js")
    css, _ = serve.static_file("/dashboard/style.css")
    assert b'id="gridHint"' in index
    assert b"var DENSE_FROM = 100;" in app and b'classList.toggle("dense", dense)' in app
    assert b"function rotationResult(" in app
    assert b".grid.dense" in css and b".tile.rot" in css


def test_the_page_script_parses():
    """app.js has no build step; a syntax error would only show in the browser."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed on this machine")
    result = subprocess.run([node, "--check", str(ROOT / "dashboard" / "static" / "app.js")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


# -- runbook v1.2 (2026-10-14): the animated page, attack tiles, gradual connect ----


def test_chargers_connect_one_by_one_and_then_start_charging():
    """--mock: the page starts empty, tiles pop in, then turn green (V1)."""
    from dashboard.mock import CHARGE_DELAY_S

    clock = Clock()
    fleet = MockFleet(n=9, connect_over_s=4.0, clock=clock)
    first = fleet.fleet()
    assert first["connected_count"] == 1                      # only the Pi at t = 0
    assert {s["connection_state"] for s in first["stations"][1:]} == {"never_seen"}
    assert first["charging_count"] == 0
    clock.advance(2.0)
    middle = fleet.fleet()["connected_count"]
    assert 1 < middle < 10
    clock.advance(2.0 + CHARGE_DELAY_S + 0.1)
    last = fleet.fleet()
    assert last["connected_count"] == 10 and last["charging_count"] == 10
    kinds = [e["event_type"] for e in fleet.events_after(0, 2000)["events"]]
    assert kinds.count("connection_established") == 10


def test_a_never_seen_mock_row_has_the_same_fields():
    fleet = MockFleet(n=2, connect_over_s=10.0, clock=Clock())
    rows = fleet.fleet()["stations"]
    assert set(rows[0]) == set(rows[1]) == set(rows[2])


def test_mock_intruder_is_the_servers_tls_refusal_line():
    """Same shape as csms/server.py _tls_refused: no station id, reason tls_rejected."""
    from csms.transport import TLS_REFUSAL_WORDS

    fleet = MockFleet(n=2, clock=Clock())
    assert serve.mock_api(fleet, "/api/mock/intruder", {})[0] == 200
    ev = fleet.events_after(0, 2000)["events"][-1]
    assert ev["event_type"] == "connection_failed" and ev["station_id"] is None
    assert ev["outcome"] == "rejected"
    p = ev["payload"]
    assert p["reason"] == "tls_rejected" and p["detail"] == TLS_REFUSAL_WORDS[p["tls_error"]]


def test_mock_wrong_identity_is_the_servers_identity_check_line():
    fleet = MockFleet(n=2, clock=Clock())
    status, body = serve.mock_api(fleet, "/api/mock/wrong-identity", {})
    assert status == 200 and body["certificate_of"] == "CP0004"
    ev = fleet.events_after(0, 2000)["events"][-1]
    assert (ev["event_type"], ev["station_id"], ev["outcome"]) == ("connection_attempt", "CP0045", "rejected")
    assert ev["payload"]["transition"] == "identity_check"
    assert ev["payload"]["certificate_common_name"] == "CP0004"
    assert ev["payload"]["identity_matches"] is False


def test_the_page_has_the_v12_pieces():
    """The redesign is wired: empty state, stepper, alert banner, summary, timeline,
    attack handling, wave replay, mock attack buttons and the colour per state."""
    index, _ = serve.static_file("/dashboard/index.html")
    app, _ = serve.static_file("/dashboard/app.js")
    css, _ = serve.static_file("/dashboard/style.css")
    for el in (b'id="empty"', b'id="stepper"', b'id="alertBar"', b'id="summary"',
               b'id="cTimeline"', b'id="btnIntruder"', b'id="btnWrongId"'):
        assert el in index, el
    for code in (b'"tls_rejected"', b'"identity_check"', b'"pq_auth_failed"', b"var HOLD_MS =",
                 b"/api/mock/intruder", b"/api/mock/wrong-identity", b"function renderSummary(",
                 b'"never_seen"'):
        assert code in app, code
    for state in (b"idle", b"charging", b"migrating", b"hybrid", b"rolled", b"legacy", b"offline", b"alert"):
        assert b"--v-" + state + b":" in css and b".tile.v-" + state in css, state


def test_serve_mock_defaults_to_a_gradual_start():
    args = serve.build_parser().parse_args(["--mock"])
    assert args.mock_connect_s > 0
    assert serve.build_parser().parse_args(["--mock", "--mock-connect-s", "0"]).mock_connect_s == 0
