"""
C-F5 part 2 (L38): one run started as two load generators is analysed as ONE run.
Track C (tests; tracker step 8, T-1).

The halt run is started as `--n 20` + `--n 25 --start-index 26` (Track A's
refusers CP0021-25 in between) and the demo fleet as `--n 44` + `--n 4
--start-index 46`. analysis/collect.py join_split_runs() must put such parts
back together -- and must never join two runs that were not started together.
"""

from __future__ import annotations

from analysis import collect, run as arun
from tests.analysis.fakes import T0, Diaries


def _runs(d: Diaries) -> list[collect.HarnessRun]:
    _events, logs = d.write()
    files = [collect.read_harness_file(p) for p in collect.find_harness_files(logs)]
    return collect.all_harness_runs(files)


def test_the_halt_run_started_in_two_parts_is_one_run_of_45(tmp_path):
    d = Diaries(tmp_path)
    d.fleet_run(experiment="halt_sw2", n=20, mode="hybrid", run_id="a", first_id=1)
    d.fleet_run(experiment="halt_sw2", n=25, mode="hybrid", run_id="b", first_id=26, start=T0 + 0.5)
    _events, logs = d.write()

    results = arun.build(None, logs)
    assert results["slot_order"] == ["halt_sw2|n45|hybrid|tls"]
    slot = results["slots"]["halt_sw2|n45|hybrid|tls"]
    assert [p["n_stations"] for p in slot["joined_from"]] == [20, 25]
    assert slot["harness_run_id"] == "a+b" and slot["completed"] is True
    assert slot["overview"]["stations"]["spawned"] == 45
    assert (results["sources"]["tester_runs"], results["sources"]["runs_joined"]) == (2, 1)


def test_demo_and_demo_legacy_are_joined(tmp_path):
    d = Diaries(tmp_path)
    d.fleet_run(experiment="demo1", n=44, mode="hybrid", run_id="a", first_id=1)
    d.fleet_run(experiment="demo1_legacy", n=4, mode="hybrid", run_id="b", first_id=46, start=T0 + 0.3)
    [joined] = collect.join_split_runs(_runs(d))
    assert (joined.experiment, joined.n_stations, len(joined.station_ids)) == ("demo1", 48, 48)


def test_a_joined_run_is_complete_only_if_every_part_finished(tmp_path):
    d = Diaries(tmp_path)
    d.fleet_run(experiment="halt", n=3, mode="hybrid", run_id="a", first_id=1)
    d.fleet_run(experiment="halt", n=3, mode="hybrid", run_id="b", first_id=10, start=T0 + 0.2,
                stop_early=True)                              # this part was stopped with Ctrl-C
    [joined] = collect.join_split_runs(_runs(d))
    assert joined.parts and joined.completed is False


def test_runs_that_were_not_started_together_stay_apart(tmp_path):
    d = Diaries(tmp_path)
    # same name, same chargers, at the same time: overlapping ids -> two different runs
    d.fleet_run(experiment="e1", n=5, mode="hybrid", run_id="a", first_id=1)
    d.fleet_run(experiment="e1", n=6, mode="hybrid", run_id="b", first_id=1, start=T0 + 0.1)
    # a different experiment name at the same time (halt_a vs halt_b do not match)
    d.fleet_run(experiment="halt_a", n=3, mode="hybrid", run_id="c", first_id=100, start=T0)
    d.fleet_run(experiment="halt_b", n=3, mode="hybrid", run_id="d", first_id=200, start=T0)
    # another mode, and a run an hour earlier
    d.fleet_run(experiment="e1", n=2, mode="classical", run_id="e", first_id=300, start=T0)
    d.fleet_run(experiment="e1", n=2, mode="hybrid", run_id="f", first_id=400, start=T0 - 3600)
    runs = _runs(d)
    out = collect.join_split_runs(runs)
    assert len(out) == len(runs) and not any(r.parts for r in out)
