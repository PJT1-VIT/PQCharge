"""
C6.2 — A RUN STOPPED EARLY KEEPS ITS CONNECTION TIMES. Track C (tests).

What these guard: since C6.2 the load generator writes a station_connected
line the moment each charger connects. The analysis must read E1 from those
lines when the tester was stopped (Ctrl-C) before the chargers reported,
must not count a connection twice when both sources exist, and must still
read old diaries (no station_connected lines) exactly as before.
"""

from __future__ import annotations

from analysis import check, collect, match
from analysis.collect import HarnessRun
from analysis.measures import e1_handshake, e2_storm, overview
from tests.analysis.fakes import Diaries


def _run(tmp_path, **kw):
    d = Diaries(tmp_path)
    d.fleet_run(experiment="e1", n=3, mode="classical", connect_ms=[30.0, 40.0, 50.0], **kw)
    events, logs = d.write()
    diary = collect.read_server_diary(events)
    run = collect.read_harness_file(logs / "e1_n3_classical.jsonl").runs[0]
    return match.match_run(run, diary)


def _trust(m):
    return check.check_run(m, overview.measure(m), e1_handshake.measure(m))


def _codes(trust):
    return [i["code"] for i in trust["issues"]]


# -- the s1b case, fixed -------------------------------------------------------


def test_early_stop_keeps_e1_from_station_connected_lines(tmp_path):
    m = _run(tmp_path, connected_lines=True, stop_early=True)
    assert not m.harness.completed
    assert not m.harness.of_type("station_finished")

    e1 = e1_handshake.measure(m)
    assert e1["station_side_available"] is True
    assert e1["station_connect_ms"]["n"] == 3
    assert e1["station_connect_ms"]["median"] == 40.0

    trust = _trust(m)
    assert trust["status"] == "warn"
    assert "run_incomplete" in _codes(trust)
    assert "no_station_side_timing" not in _codes(trust)
    unreported = next(i for i in trust["issues"] if i["code"] == "unreported_stations")
    assert "stopped early" in unreported["message"]


def test_early_stop_on_an_old_diary_says_why_e1_is_missing(tmp_path):
    """The real s1b run: stopped before any charger reported, no C6.2 lines."""
    m = _run(tmp_path, stop_early=True)
    e1 = e1_handshake.measure(m)
    assert e1["station_side_available"] is False

    issue = next(i for i in _trust(m)["issues"] if i["code"] == "no_station_side_timing")
    assert "stopped before the chargers reported" in issue["message"]
    assert "recorded before chargers logged" not in issue["message"]


def test_a_completed_old_run_keeps_the_old_message(tmp_path):
    m = _run(tmp_path, omit_connect_ms=True)
    issue = next(i for i in _trust(m)["issues"] if i["code"] == "no_station_side_timing")
    assert "recorded before chargers logged" in issue["message"]


# -- no double counting -------------------------------------------------------


def test_both_sources_present_count_each_connection_once(tmp_path):
    m = _run(tmp_path, connected_lines=True)
    assert m.harness.completed
    e1 = e1_handshake.measure(m)
    assert e1["station_connect_ms"]["n"] == 3
    assert e1["reconnect_connect_ms"]["n"] == 0
    assert _trust(m)["status"] == "pass"


def test_reconnections_after_a_storm_come_from_the_live_lines_too(tmp_path):
    storm = {"kill_at": 2.0, "outage": 1.0, "recovery": [0.5, 0.6, 0.7]}
    m = _run(tmp_path, connected_lines=True, stop_early=True, storm=storm)
    e1 = e1_handshake.measure(m)
    assert e1["station_connect_ms"]["n"] == 3
    assert e1["reconnect_connect_ms"]["n"] == 3
    assert e2_storm.measure(m)["reconnect_connect_ms"]["n"] == 3


# -- HarnessRun.connect_times, the merge rule ------------------------------------


def _harness(rows):
    return HarnessRun(path="x", run_id="r", rows=rows)


def test_the_source_with_more_entries_wins():
    rows = [
        # one live line reached the disk, the final row has both connections
        {"event_type": "station_connected", "station_id": "CP0001", "connect_ms": 10.0, "connection": 1},
        {"event_type": "station_finished", "station_id": "CP0001", "connect_ms": [10.0, 30.0]},
        # the final row never came; two live lines did
        {"event_type": "station_connected", "station_id": "CP0002", "connect_ms": 11.0, "connection": 1},
        {"event_type": "station_connected", "station_id": "CP0002", "connect_ms": 33.0, "connection": 2},
    ]
    assert _harness(rows).connect_times() == {
        "CP0001": [10.0, 30.0],
        "CP0002": [11.0, 33.0],
    }


def test_live_lines_are_ordered_by_connection_number():
    rows = [
        {"event_type": "station_connected", "station_id": "CP0001", "connect_ms": 99.0, "connection": 2},
        {"event_type": "station_connected", "station_id": "CP0001", "connect_ms": 12.0, "connection": 1},
    ]
    assert _harness(rows).connect_times() == {"CP0001": [12.0, 99.0]}


def test_an_old_diary_reads_exactly_as_before():
    rows = [
        {"event_type": "station_finished", "station_id": "CP0001", "connect_ms": [5.0]},
        {"event_type": "station_finished", "station_id": "CP0002", "connect_ms": []},
        {"event_type": "station_finished", "station_id": "CP0003"},  # pre-C5.1 row
    ]
    assert _harness(rows).connect_times() == {"CP0001": [5.0], "CP0002": []}
