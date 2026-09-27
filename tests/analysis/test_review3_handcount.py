"""
The hand-counted check: the analysis against a REAL recorded run.

Track C (tests). Phase C6. Design document §14.1: "Validate the event log
against a hand-counted run ... an hour's work that prevents a lost week."

The fixtures are unmodified copies of the Review 3 run (16 Sep 2026):
25 simulated chargers against the live CSMS, classical, no TLS.

    tests/fixtures/analysis/review3_events.jsonl        server diary (Track A)
    tests/fixtures/analysis/review3_n25_classical.jsonl tester diary (Track C)

Every expected number below was counted by hand from those files with
plain text search (grep), independently of the analysis code:

    grep '"transaction_ended"'   ... CP00xx   -> 25 lines
    grep '"transaction_started"' ... CP00xx   -> 25 lines
    grep '"transaction_updated"' ... CP00xx   -> 200 lines
    grep '"connection_established"' CP00xx   -> 25 lines
    sum of energy_wh on the 25 ended lines    -> 411.4266 Wh

The server diary also holds one extra smoke-test session by CP001 (the old
3-digit id) at the same time. It is NOT part of the tester's run and must
appear as an external charger, not inside the run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from analysis import run

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "analysis"


@pytest.fixture(scope="module")
def results():
    return run.build(
        FIX / "review3_events.jsonl",
        FIX,
        [FIX / "review3_n25_classical.jsonl"],
    )


def test_the_run_is_found_and_paired(results):
    assert results["slot_order"] == ["review3|n25|classical|plain"]
    slot = results["slots"]["review3|n25|classical|plain"]
    assert slot["harness_run_id"] == "4f2bc4b0cf8a"
    assert slot["server_run_ids"] == ["abf36d200272"]


def test_the_counts_match_the_hand_count(results):
    ov = results["slots"]["review3|n25|classical|plain"]["overview"]
    assert ov["stations"]["spawned"] == 25
    assert ov["stations"]["finished_ok"] == 25
    assert ov["sessions"]["started"] == 25
    assert ov["sessions"]["ended"] == 25
    assert ov["meter"]["readings"] == 25 + 200 + 25
    assert ov["connections"]["server_established"] == 25
    assert ov["sessions"]["energy_wh_total"] == pytest.approx(411.4266, abs=1e-3)
    assert ov["meter"]["missing_events"] == 0


def test_the_fleet_peaked_at_25_chargers_at_7_4_kw(results):
    power = results["slots"]["review3|n25|classical|plain"]["overview"]["timeline"]["power_w"]
    assert max(v for _, v in power) == pytest.approx(25 * 7400.0)


def test_the_smoke_test_charger_is_external_not_part_of_the_run(results):
    assert [n["station_id"] for n in results["external_nodes"]] == ["CP001"]


def test_the_only_caveat_is_that_the_run_predates_station_side_timing(results):
    trust = results["slots"]["review3|n25|classical|plain"]["trust"]
    assert trust["status"] == "warn"
    assert [i["code"] for i in trust["issues"]] == ["no_station_side_timing"]


def test_no_storm_and_no_migration_means_no_e2_and_no_e3(results):
    slot = results["slots"]["review3|n25|classical|plain"]
    assert slot["e2"] is None and slot["e3"] is None
