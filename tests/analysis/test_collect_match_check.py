"""
COLLECT, MATCH and CHECK (analysis/collect.py, match.py, check.py). Track C, Phase C6.

What these guard: the diaries are read without silently losing lines, each
tester run is paired with exactly its own server lines, and a run whose
numbers cannot be trusted says so.
"""

from __future__ import annotations

import json

from analysis import check, collect, match
from analysis.measures import e1_handshake, overview
from tests.analysis.fakes import T0, Diaries


def _one_run(tmp_path, **kw):
    d = Diaries(tmp_path)
    d.fleet_run(experiment="e1", n=3, mode="classical", **kw)
    events, logs = d.write()
    diary = collect.read_server_diary(events)
    run = collect.read_harness_file(logs / "e1_n3_classical.jsonl").runs[0]
    return diary, run, match.match_run(run, diary)


# -- COLLECT -----------------------------------------------------------------


def test_a_half_written_last_line_is_counted_and_skipped(tmp_path):
    d = Diaries(tmp_path)
    d.fleet_run(experiment="e1", n=2, mode="classical")
    events, _ = d.write(truncate_server=True)
    diary = collect.read_server_diary(events)
    assert diary.lines_unreadable == 1 and diary.last_line_unreadable
    status = check.check_diary(diary)
    assert status["status"] == "pass"
    assert status["issues"][0]["code"] == "truncated_last_line"


def test_a_new_top_level_field_is_kept_and_reported_not_silently_dropped(tmp_path):
    """
    csms.events.read_events would skip EVERY such line (Event(**fields) raises).
    Here they are kept, counted, and the trust check warns.
    """
    d = Diaries(tmp_path)
    d.fleet_run(experiment="e1", n=2, mode="classical")
    events, _ = d.write()
    lines = events.read_text().splitlines()
    lines = [json.dumps({**json.loads(line), "new_field": 1}) for line in lines]
    events.write_text("\n".join(lines) + "\n")

    diary = collect.read_server_diary(events)
    assert len(diary.events) == len(lines)
    assert diary.unknown_fields == {"new_field": len(lines)}
    assert check.check_diary(diary)["status"] == "warn"


def test_two_runs_appended_to_one_tester_file_are_separated(tmp_path):
    d = Diaries(tmp_path)
    d.fleet_run(experiment="e1", n=2, mode="pqc", run_id="first")
    d.fleet_run(experiment="e1", n=2, mode="pqc", run_id="second", start=T0 + 100)
    _, logs = d.write()
    runs = collect.read_harness_file(logs / "e1_n2_pqc.jsonl").runs
    assert [r.run_id for r in runs] == ["first", "second"]
    assert all(len(r.station_ids) == 2 for r in runs)


def test_only_files_named_by_the_agreed_pattern_are_picked_up(tmp_path):
    (tmp_path / "e1_n5_pqc.jsonl").write_text("")
    (tmp_path / "events.jsonl").write_text("")
    (tmp_path / "agent_CP0001.log").write_text("")
    assert [p.name for p in collect.find_harness_files(tmp_path)] == ["e1_n5_pqc.jsonl"]


def test_a_missing_server_diary_is_a_failure_that_says_where_it_looked(tmp_path):
    diary = collect.read_server_diary(tmp_path / "nope.jsonl")
    result = check.check_diary(diary)
    assert result["status"] == "fail"
    assert "nope.jsonl" in result["issues"][0]["message"]


# -- MATCH -------------------------------------------------------------------


def test_a_run_gets_its_own_chargers_lines_and_nobody_elses(tmp_path):
    d = Diaries(tmp_path)
    d.fleet_run(experiment="e1", n=3, mode="classical")
    # Another charger, same moment, not part of this run (the Pi, say).
    d.server(T0 + 1, "connection_established", "CP0100", run_id="srv1")
    # One of ours, but an hour later: a different run.
    d.server(T0 + 3600, "connection_established", "CP0001", run_id="srv9")
    events, logs = d.write()
    diary = collect.read_server_diary(events)
    run = collect.read_harness_file(logs / "e1_n3_classical.jsonl").runs[0]
    m = match.match_run(run, diary)

    ids = {e["station_id"] for e in m.station_events}
    assert ids == {"CP0001", "CP0002", "CP0003"}
    assert "srv9" not in m.server_run_ids


def test_a_clock_disagreement_leaves_the_run_unmatched_and_the_check_fails(tmp_path):
    d = Diaries(tmp_path)
    d.fleet_run(experiment="e1", n=2, mode="classical")
    for line in d.server_lines:          # server clock 10 minutes fast
        from tests.analysis.fakes import iso
        line["timestamp"] = iso(collect.to_epoch(line["timestamp"]) + 600)
    events, logs = d.write()
    diary = collect.read_server_diary(events)
    run = collect.read_harness_file(logs / "e1_n2_classical.jsonl").runs[0]
    m = match.match_run(run, diary)
    assert m.station_events == []
    trust = check.check_run(m, overview.measure(m), e1_handshake.measure(m))
    assert trust["status"] == "fail"
    assert any(i["code"] == "no_server_data" for i in trust["issues"])


# -- CHECK -------------------------------------------------------------------


def test_a_clean_run_is_trusted(tmp_path):
    _, _, m = _one_run(tmp_path)
    trust = check.check_run(m, overview.measure(m), e1_handshake.measure(m))
    assert trust["status"] == "pass", trust


def test_a_crashed_charger_program_makes_the_run_unusable(tmp_path):
    _, _, m = _one_run(tmp_path, crash="CP0002")
    trust = check.check_run(m, overview.measure(m), e1_handshake.measure(m))
    assert trust["status"] == "fail"
    assert any(i["code"] == "agent_crashed" for i in trust["issues"])


def test_a_run_from_before_the_c5_fix_is_flagged_not_hidden(tmp_path):
    _, _, m = _one_run(tmp_path, omit_connect_ms=True)
    e1 = e1_handshake.measure(m)
    assert e1["station_side_available"] is False
    trust = check.check_run(m, overview.measure(m), e1)
    assert trust["status"] == "warn"
    assert any(i["code"] == "no_station_side_timing" for i in trust["issues"])
