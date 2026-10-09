"""
C-P5 — the live migration view (harness/watch_migration.py). Track C (tests).

What these prove:
  * the screen says, in plain words, what the migration is doing, and every
    number on it is the server's own (counts, waves) -- never recomputed;
  * changes between polls are listed ("in progress -> upgraded (key check
    passed)"), and the view stops by itself when the migration it watched
    ends -- but NOT for an earlier, already-finished migration on the same
    server;
  * an unreachable server is shown and retried, never a crash;
  * Ctrl-C exits cleanly with 130;
  * plain ASCII only (PowerShell 5);
  * end to end over real HTTP, against a small server that plays back a
    migration exactly as /api/fleet returns it.

Uses no real CSMS: the JSON shapes are copied from csms/fleet.py
(FleetSnapshot.to_dict) and idmanager/api.py (MigrationStatus.to_dict).
"""

from __future__ import annotations

import io
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from harness import watch_migration as wm

T0 = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)


def iso(sec: float) -> str:
    return (T0 + timedelta(seconds=sec)).isoformat()


def station(sid: str, state: str = "pending", connected: bool = True) -> dict:
    return {"station_id": sid, "migration_state": state,
            "connection_state": "connected" if connected else "disconnected",
            "boot_accepted": connected}


def migration(phase: str, mid: str = "m1", **counts) -> dict:
    base = {"migration_id": mid if phase != "idle" else "", "target_mode": "pqc", "phase": phase,
            "total_stations": 4, "pending": 4, "in_progress": 0, "migrated": 0,
            "rolled_back": 0, "incompatible": 0, "current_wave": None, "total_waves": 0,
            "waves": [], "started_at": None if phase == "idle" else iso(0), "completed_at": None}
    base.update(counts)
    return base


def fleet(states: dict[str, str], mig: dict, offline=()) -> dict:
    return {"run_id": "srv1", "crypto_mode": "classical", "total_stations": len(states),
            "connected_count": len(states) - len(offline), "booted_count": len(states),
            "charging_count": len(states) - len(offline), "aggregate_power_w": 7400.0 * len(states),
            "stations": [station(s, st, s not in offline) for s, st in states.items()],
            "migration": mig}


IDS = ["CP0001", "CP0002", "CP0003", "CP0004"]
WAVE0 = {"wave_id": 0, "is_canary": True, "station_ids": ["CP0001"], "phase": "completed",
         "migrated_count": 1, "failed_count": 0, "started_at": iso(0), "completed_at": iso(0.4)}
WAVE1 = {"wave_id": 1, "is_canary": False, "station_ids": ["CP0002", "CP0003"], "phase": "rolled_back",
         "migrated_count": 0, "failed_count": 2, "started_at": iso(0.4), "completed_at": iso(1.0)}

SCRIPT = [
    fleet(dict.fromkeys(IDS, "pending"), migration("idle")),
    fleet({"CP0001": "in_progress", "CP0002": "pending", "CP0003": "pending", "CP0004": "incompatible"},
          migration("canary", pending=2, in_progress=1, incompatible=1, total_waves=2)),
    fleet({"CP0001": "migrated", "CP0002": "in_progress", "CP0003": "in_progress", "CP0004": "incompatible"},
          migration("running", pending=0, in_progress=2, migrated=1, incompatible=1, total_waves=2,
                    current_wave=1, waves=[WAVE0, dict(WAVE1, phase="running", completed_at=None,
                                                       failed_count=0)])),
    fleet({"CP0001": "migrated", "CP0002": "rolled_back", "CP0003": "rolled_back", "CP0004": "incompatible"},
          migration("rolled_back", pending=0, migrated=1, rolled_back=2, incompatible=1, total_waves=2,
                    waves=[WAVE0, WAVE1], completed_at=iso(1.0))),
]


def polls(script):
    return [wm.Poll(fleet=f, migration=f["migration"]) for f in script]


# -- the screen ------------------------------------------------------------------


def test_an_idle_server_says_it_is_waiting():
    tracker = wm.Tracker()
    poll = polls(SCRIPT)[0]
    tracker.update(poll)
    text = wm.render(poll, tracker, now_utc=T0)
    assert "IDLE" in text and "waiting for a migration to start" in text
    assert not tracker.finished


def test_the_screen_shows_the_servers_own_numbers():
    tracker = wm.Tracker()
    for p in polls(SCRIPT):
        tracker.update(p)
    text = wm.render(polls(SCRIPT)[-1], tracker, now_utc=T0 + timedelta(seconds=5))
    assert "Phase: ROLLED_BACK" in text and "rolled back; migration stopped" in text
    assert "upgraded 1   in progress 0   waiting 0   rolled back 2   incompatible 1" in text
    assert "4/4 decided" in text
    assert "canary  CP0001" in text and "wave 1  CP0002-CP0003" in text
    assert "time: 1.0 s" in text                 # completed_at - started_at, not "now"
    assert "power 29.6 kW" in text
    # One letter per charger: U, R, R, x.
    assert "CP0001   URRx" in text


def test_changes_between_polls_are_listed_in_plain_words():
    tracker = wm.Tracker()
    for k, p in enumerate(polls(SCRIPT)):
        tracker.update(p, now=float(k))
    notes = [(sid, old, new) for _t, sid, old, new in tracker.changes]
    assert ("CP0001", "in_progress", "migrated") in notes
    assert ("CP0002", "in_progress", "rolled_back") in notes
    text = wm.render(polls(SCRIPT)[-1], tracker, now_utc=T0)
    assert "CP0001   in progress -> upgraded  (key check passed)" in text
    assert "CP0002   in progress -> rolled back  (failed, rolled back)" in text


def test_an_offline_waiting_charger_is_a_dash():
    poll = wm.Poll(fleet=fleet(dict.fromkeys(IDS, "pending"), migration("canary"), offline={"CP0003"}),
                   migration=migration("canary"))
    text = wm.render(poll, wm.Tracker(), now_utc=T0)
    assert "CP0001   ..-." in text


def test_the_screen_is_plain_ascii():
    tracker = wm.Tracker()
    for p in polls(SCRIPT):
        tracker.update(p)
    text = wm.render(polls(SCRIPT)[-1], tracker, now_utc=T0) + wm.summary(polls(SCRIPT)[-1])
    text.encode("ascii")                         # raises if not


def test_an_unreachable_server_is_shown_not_fatal():
    poll = wm.fetch("http://127.0.0.1:9", timeout_s=0.5)
    assert poll.error and poll.fleet is None
    text = wm.render(poll, wm.Tracker(), url="http://127.0.0.1:9", now_utc=T0)
    assert "server not reachable at http://127.0.0.1:9 (retrying)" in text
    assert "no answer from the server yet" in text and "IDLE" not in text


def test_the_summary_names_the_outcome():
    text = wm.summary(polls(SCRIPT)[-1])
    assert "ended: ROLLED_BACK" in text
    assert "1 upgraded, 2 rolled back, 1 incompatible, 0 still waiting, of 4; 2 wave(s), 1 rolled back." in text


# -- when to stop -----------------------------------------------------------------


def test_it_stops_after_the_migration_it_watched():
    tracker = wm.Tracker()
    for p in polls(SCRIPT)[:-1]:
        tracker.update(p)
        assert not tracker.finished
    tracker.update(polls(SCRIPT)[-1])
    assert tracker.finished


def test_an_earlier_finished_migration_does_not_stop_it():
    """Same server, second migration: the first one's end must not end the view."""
    tracker = wm.Tracker()
    old = polls(SCRIPT)[-1]                      # m1, already rolled back
    tracker.update(old)
    tracker.update(old)
    assert not tracker.finished and tracker.waiting_for_new
    assert "waiting for a NEW migration" in wm.render(old, tracker, now_utc=T0)

    new_running = [wm.Poll(fleet=f, migration=dict(f["migration"], migration_id="m2"))
                   for f in SCRIPT[1:]]
    for p in new_running[:-1]:
        tracker.update(p)
        assert not tracker.finished
    tracker.update(new_running[-1])
    assert tracker.finished and tracker.watching_id == "m2"


# -- the console ------------------------------------------------------------------


def test_a_non_terminal_stream_is_never_redrawn():
    assert wm.enable_redraw(io.StringIO()) is False


def test_ctrl_c_exits_cleanly(monkeypatch):
    monkeypatch.setattr(wm, "fetch", lambda *a, **k: polls(SCRIPT)[0])

    def interrupt(_s):
        raise KeyboardInterrupt

    monkeypatch.setattr(wm.time, "sleep", interrupt)
    out = io.StringIO()
    code = wm.watch(wm.build_parser().parse_args(["--no-clear"]), out=out)
    assert code == 130 and "stopped (Ctrl-C)" in out.getvalue()
    assert "Traceback" not in out.getvalue()


# -- end to end over HTTP ------------------------------------------------------------


class Playback(BaseHTTPRequestHandler):
    script: list = []
    calls = 0

    def do_GET(self):  # noqa: N802 - http.server API
        if self.path != "/api/fleet":
            self.send_response(404)
            self.end_headers()
            return
        k = min(Playback.calls, len(Playback.script) - 1)
        Playback.calls += 1
        body = json.dumps(Playback.script[k]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence
        pass


@pytest.fixture
def playback():
    Playback.script, Playback.calls = SCRIPT, 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), Playback)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_end_to_end_it_follows_a_migration_and_exits_by_itself(playback):
    out = io.StringIO()
    code = wm.watch(wm.build_parser().parse_args(
        ["--url", playback, "--every", "0.01", "--no-clear"]), out=out)
    text = out.getvalue()
    assert code == 0
    assert Playback.calls == len(SCRIPT)          # stopped at the end, no extra polls
    assert "waiting for a migration to start" in text
    assert "Phase: CANARY" in text and "Phase: RUNNING" in text
    assert "ended: ROLLED_BACK" in text
    assert "(key check passed)" in text


def test_a_lost_poll_keeps_the_last_picture(monkeypatch):
    seq = iter([polls(SCRIPT)[2], wm.Poll(error="URLError: refused"), polls(SCRIPT)[-1]])
    monkeypatch.setattr(wm, "fetch", lambda *a, **k: next(seq))
    monkeypatch.setattr(wm.time, "sleep", lambda _s: None)
    out = io.StringIO()
    assert wm.watch(wm.build_parser().parse_args(["--no-clear"]), out=out) == 0
    screens = out.getvalue().split("-" * 78)
    assert "server not reachable" in screens[1] and "Phase: RUNNING" in screens[1]
    assert "ended: ROLLED_BACK" in out.getvalue()
