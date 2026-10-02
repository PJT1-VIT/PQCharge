"""
Phase C6.1 — the analysis, ready for the A+B+C integration session.

Track C (tests).

Each test pins one problem found while planning the session:

    I1  the charger id of Track B's lines is in payload.station
    I2  every run writes its own server diary; all must be read, once each
    I3  E3 counts the WHOLE fleet (Stage 6's failing chargers are Track A's)
    I4  the check result is in payload.result
    I5  "key installed" is not "authenticated"
    I6  energy from chargers we did not start is labelled, never summed
    F9  a crashed migration controller makes the run unusable
    --  the controller's counts must add up
    --  the charger's own view must agree with the server's
    S2  the key-check round-trip time is reported
"""

from __future__ import annotations

import json

import pytest

from analysis import collect, match, run
from analysis.measures import e3_migration, e5_security, e6_nodes
from tests.analysis.fakes import T0, Diaries


def _matched(d: Diaries, name: str):
    events, logs = d.write()
    diary = collect.read_server_diary(events)
    harness = collect.read_harness_file(logs / name).runs[0]
    return match.match_run(harness, diary), diary


# -- I1 / I4: the charger and the result live in the payload -----------------


def test_orchestrator_lines_get_their_charger_and_result_from_the_payload(tmp_path):
    d = Diaries(tmp_path)
    d.fleet_run(experiment="e3", n=8, mode="pqc", migrate=True, charge_s=8)
    events, _ = d.write()
    diary = collect.read_server_diary(events)
    checks = [e for e in diary.events if (e["payload"] or {}).get("transition") == "pq_auth"]
    assert checks, "fakes write pq_auth lines"
    for e in checks:
        assert e["station_id"] == e["payload"]["station"]
        assert e["outcome"] == "success" and e.get("_normalised") is True
    assert diary.normalised == len(checks)


def test_a_filled_top_level_field_is_never_overwritten(tmp_path):
    """If Track A later copies station/result up themselves, nothing changes."""
    d = Diaries(tmp_path)
    d.server(T0, "connection_attempt", "CP0001", run_id="s", outcome="rejected",
             transition="pq_auth", station="CP9999", result="success")
    events, _ = d.write()
    ev = collect.read_server_diary(events).events[0]
    assert ev["station_id"] == "CP0001" and ev["outcome"] == "rejected"


# -- I2: several server diaries, each line once ---------------------------


def test_every_server_diary_is_read_and_a_copied_line_counts_once(tmp_path):
    d = Diaries(tmp_path)
    d.fleet_run(experiment="s1", n=3, mode="classical")
    events, logs = d.write()
    lines = events.read_text().splitlines()
    half = len(lines) // 2
    (logs / "s1_events.jsonl").write_text("\n".join(lines[:half]) + "\n")
    (logs / "s1b_events.jsonl").write_text("\n".join(lines[half - 5:]) + "\n")   # 5 overlap
    events.unlink()

    results = run.build(None, logs)
    assert results["sources"]["server_diaries"] == [
        str(logs / "s1_events.jsonl"), str(logs / "s1b_events.jsonl"),
    ]
    assert results["sources"]["server_duplicates_dropped"] == 5
    slot = results["slots"]["s1|n3|classical|tls"]
    assert slot["overview"]["sessions"]["ended"] == 3, "nothing lost, nothing doubled"


def test_the_automatic_run_reads_the_server_diary_it_is_given(tmp_path):
    """The runbook's server writes logs/stage6_events.jsonl; --events-log names it."""
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="stage6", n=3, mode="classical")
    events, logs = d.write()
    elsewhere = tmp_path / "server_side"
    elsewhere.mkdir()
    named = elsewhere / "stage6_events.jsonl"
    events.rename(named)
    out = tmp_path / "out"

    run.analyse_after_run(logs / info["name"], events=[str(named)], out=out)
    results = json.loads((out / "results.json").read_text())
    assert results["slots"]["stage6|n3|classical|tls"]["trust"]["status"] == "pass"


def test_the_load_generator_accepts_events_log():
    from harness.load_generator import build_parser

    args = build_parser().parse_args(
        ["--n", "1", "--events-log", "logs/s1_events.jsonl", "--events-log", "x.jsonl"]
    )
    assert args.events_log == ["logs/s1_events.jsonl", "x.jsonl"]
    assert build_parser().parse_args(["--n", "1"]).events_log is None


# -- I3: the whole fleet ----------------------------------------------------


def test_e3_counts_chargers_the_tester_did_not_start(tmp_path):
    """
    Stage 6 shape: the tester ran 4 chargers; the rolled-back wave is made of
    4 OTHER chargers (Track A's refusers), present only in the server's
    snapshot. E3 must still show 4 rolled back.
    """
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="stage6", n=8, mode="pqc", migrate=True, charge_s=8)
    # Remove CP0005-CP0008 from the tester's diary: they become "not ours".
    rows = d.harness_lines[info["name"]]
    d.harness_lines[info["name"]] = [
        r for r in rows
        if r.get("station_id") not in {"CP0005", "CP0006", "CP0007", "CP0008"}
    ]
    m, _ = _matched(d, info["name"])
    e3 = e3_migration.measure(m)
    assert e3["tester_chargers"] == 4 and e3["fleet_size"] == 8
    assert e3["final_counts"]["rolled_back"] == 4
    assert e3["final_counts"]["migrated"] == 4


# -- I5: authenticated vs key installed ---------------------------------------


def test_migrated_without_any_key_check_is_labelled_key_installed(tmp_path):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="e3", n=8, mode="pqc", migrate=True, charge_s=8, pq_checks=False)
    m, _ = _matched(d, info["name"])
    e3 = e3_migration.measure(m)
    assert e3["verification"] == "key installed (not authenticated)"
    results = run.analyse_run(m)
    codes = [i["code"] for i in results["trust"]["issues"]]
    assert "key_installed_not_authenticated" in codes
    assert results["trust"]["status"] == "warn"


def test_every_migrated_charger_with_a_passed_check_is_authenticated(tmp_path):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="e3", n=8, mode="pqc", migrate=True, charge_s=8)
    m, _ = _matched(d, info["name"])
    assert e3_migration.measure(m)["verification"] == "authenticated"


# -- S2: key-check latency ------------------------------------------------------


def test_the_key_check_round_trip_is_reported(tmp_path):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="e3", n=8, mode="pqc", migrate=True, charge_s=8)
    m, _ = _matched(d, info["name"])
    q = e3_migration.measure(m)["pq_checks"]
    assert q["total"] == 4 and q["passed"] == 4 and q["rejected"] == 0
    assert q["algorithm"] == "ML-DSA-44"
    assert q["round_trip_ms"]["n"] == 4
    assert q["round_trip_ms"]["median"] == pytest.approx((41.0 + 50.0) / 2)
    assert q["first_per_charger_ms"]["n"] == 4 and q["later_ms"] == {"n": 0}


# -- deferred chargers ------------------------------------------------------------


def test_an_offline_charger_is_skipped_not_failed(tmp_path):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="e3", n=8, mode="pqc", migrate=True, charge_s=8)
    d.server(T0 + 2.5, "station_deferred", None, run_id="srv1", source="orchestrator",
             station="CP0009", wave_id=1, reason="not connected")
    m, _ = _matched(d, info["name"])
    e3 = e3_migration.measure(m)
    assert e3["deferred"] == {"count": 1, "station_ids": ["CP0009"]}
    codes = [i["code"] for i in run.analyse_run(m)["trust"]["issues"]]
    assert "chargers_deferred" in codes


# -- F9 and the counts ------------------------------------------------------------


def test_a_crashed_migration_controller_makes_the_run_unusable(tmp_path):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="e3", n=8, mode="pqc", migrate=True, charge_s=8)
    d.server(T0 + 3.2, "migration_failed", None, run_id="srv1", source="orchestrator",
             error="PQAImportError: engine missing")
    m, _ = _matched(d, info["name"])
    trust = run.analyse_run(m)["trust"]
    assert trust["status"] == "fail"
    assert any(i["code"] == "migration_failed" and "engine missing" in i["message"]
               for i in trust["issues"])


def test_controller_counts_that_do_not_add_up_are_caught(tmp_path):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="e3", n=8, mode="pqc", migrate=True, charge_s=8, bad_counts=True)
    m, _ = _matched(d, info["name"])
    assert e3_migration.measure(m)["controller_sum_violations"] > 0
    trust = run.analyse_run(m)["trust"]
    assert trust["status"] == "fail"
    assert any(i["code"] == "migration_counts_do_not_add_up" for i in trust["issues"])


def test_correct_controller_counts_raise_nothing(tmp_path):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="e3", n=8, mode="pqc", migrate=True, charge_s=8)
    m, _ = _matched(d, info["name"])
    assert e3_migration.measure(m)["controller_sum_violations"] == 0


# -- charger's view vs server's view ---------------------------------------------


def test_a_migrated_charger_that_reports_no_key_is_flagged(tmp_path):
    d = Diaries(tmp_path)
    keys = {f"CP{i:04d}": True for i in range(1, 9)}
    keys["CP0002"] = False            # the server says migrated; the charger disagrees
    info = d.fleet_run(experiment="e3", n=8, mode="pqc", migrate=True, charge_s=8, agent_keys=keys)
    m, _ = _matched(d, info["name"])
    e3 = e3_migration.measure(m)
    assert e3["agent_view"] == {"available": True, "migrated_but_no_key": ["CP0002"]}
    codes = [i["code"] for i in run.analyse_run(m)["trust"]["issues"]]
    assert "migrated_but_charger_has_no_key" in codes


def test_a_rolled_back_charger_may_still_hold_its_key(tmp_path):
    """Rollback un-enrols on the server; the charger keeps the bytes. Not a disagreement."""
    d = Diaries(tmp_path)
    keys = {f"CP{i:04d}": True for i in range(1, 9)}
    info = d.fleet_run(experiment="e3", n=8, mode="pqc", migrate=True, charge_s=8, agent_keys=keys)
    m, _ = _matched(d, info["name"])
    assert e3_migration.measure(m)["agent_view"]["migrated_but_no_key"] == []


# -- E5 and E6 --------------------------------------------------------------------


def test_e5_counts_key_checks_and_warn_mode_mismatches(tmp_path):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="e5", n=8, mode="pqc", migrate=True, charge_s=8)
    d.server(T0 + 0.5, "connection_attempt", "CP0003", run_id="srv1", outcome="success",
             transition="identity_check", certificate_common_name="CP0004",
             identity_matches=False, identity_check="warn")
    d.server(T0 + 3.1, "connection_attempt", None, run_id="srv1", source="orchestrator",
             transition="pq_auth", station="EXT01", wave_id=2, result="rejected",
             detail="signature did not verify", duration_ms=12.0, algorithm="ML-DSA-44")
    m, _ = _matched(d, info["name"])
    e5 = e5_security.measure(m)
    assert e5["identity_checks"] == 1 and e5["identity_rejected"] == 0
    assert e5["identity_mismatches"] == 1 and e5["identity_mode"] == "warn"
    assert e5["pq_checks"] == 5 and e5["pq_passed"] == 4 and e5["pq_rejected"] == 1
    assert e5["pq_rejections"][0]["station_id"] == "EXT01"
    assert "did not verify" in e5["pq_rejections"][0]["detail"]


def test_outside_chargers_energy_is_labelled_and_key_checks_are_shown(tmp_path):
    d = Diaries(tmp_path)
    d.fleet_run(experiment="e3", n=3, mode="pqc")
    d.server(T0 + 1, "connection_established", "CP0100", run_id="hw", outcome="success")
    d.server(T0 + 2, "connection_attempt", None, run_id="hw", source="orchestrator",
             transition="pq_auth", station="CP0100", result="success", wave_id=0)
    d.server(T0 + 2, "connection_attempt", None, run_id="hw", source="orchestrator",
             transition="pq_auth", station="EXT01", result="rejected", wave_id=0)
    d.server(T0 + 3, "station_deferred", None, run_id="hw", source="orchestrator",
             station="EXT02", reason="not connected", wave_id=0)
    events, _ = d.write()
    nodes = {n["station_id"]: n for n in
             e6_nodes.measure(collect.read_server_diary(events), ["CP0001", "CP0002", "CP0003"])}
    assert nodes["CP0100"]["kind"] == "hardware" and nodes["CP0100"]["pq_auth"] == "success"
    assert nodes["EXT01"]["pq_auth"] == "rejected"
    assert nodes["EXT01"]["identity_rejected"] == 0, "a key check is not an identity check"
    assert nodes["EXT02"]["deferred"] == 1 and nodes["EXT02"]["connected"] is False
    assert all(n["energy_source"] == "charger-reported" for n in nodes.values())
