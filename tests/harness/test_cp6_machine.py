"""
C-P6 (S1) — machine readings and the "is the laptop the bottleneck?" check.
Track C (tests).

What these prove:
  * the sampler records whole-machine CPU/memory, the tester process, the
    CSMS process (found by --server-pid, by the process the harness started,
    or automatically by `-m csms.server`) and the event-loop lag;
  * a `cmd /c "python -m csms.server ..."` wrapper is never taken for the
    server;
  * the load generator writes machine_sample lines with --watch-machine
    (real agents, real sockets), and none without it;
  * the analysis summarises them, warns `machine_saturated` above the agreed
    limits (CPU p95 85 %, loop lag p95 50 ms), and builds the S1 series
    against fleet size;
  * the analysis never needs psutil.

Port 9292 (clear of 9270-9291).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

import pytest

psutil = pytest.importorskip("psutil", reason="needed for --watch-machine (requirements.txt)")

from analysis import collect, match, run  # noqa: E402
from analysis.measures import compare  # noqa: E402
from analysis.measures import machine as mm  # noqa: E402
from harness import load_generator as lg  # noqa: E402
from harness import machine  # noqa: E402
from harness import timing_log as tl  # noqa: E402
from harness.timing_log import TimingLog  # noqa: E402
from tests.analysis.fakes import T0, Diaries  # noqa: E402
from tests.fixtures.fake_csms import FakeCSMS  # noqa: E402


def make_log(tmp_path, n=1):
    return TimingLog(tmp_path / "m.jsonl", run_id="mrun", experiment="s1", n_stations=n)


def sleeper(*extra_args: str) -> subprocess.Popen:
    """A harmless child process whose command line we control."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", *extra_args])


# -- finding the server --------------------------------------------------------------


def test_the_server_is_recognised_by_its_module_argument():
    assert machine.is_server_cmdline([sys.executable, "-m", "csms.server", "--db", "x.db"])
    # A cmd /c wrapper carries it only inside one long string: not the server.
    assert not machine.is_server_cmdline(["cmd", "/c", "python -m csms.server --db x.db"])
    assert not machine.is_server_cmdline(None)
    assert not machine.is_server_cmdline([])


def test_one_sample_has_every_reading(tmp_path):
    w = machine.MachineWatcher(make_log(tmp_path))
    row = w.sample(loop_lag_ms=3.21)
    assert 0.0 <= row["cpu_pct"] <= 100.0 and row["cpu_count"] >= 1
    assert 0.0 < row["mem_pct"] <= 100.0 and row["mem_used_mb"] > 0
    assert row["loop_lag_ms"] == 3.21
    assert row["tester"]["pid"] == os.getpid() and row["tester"]["rss_mb"] > 0
    assert row["tester"]["threads"] >= 1


def test_an_explicit_server_pid_is_sampled(tmp_path):
    child = sleeper()
    try:
        w = machine.MachineWatcher(make_log(tmp_path), server_pid=child.pid)
        assert w.sample(0.0)["server"]["pid"] == child.pid
    finally:
        child.kill()
        child.wait()


def test_the_process_the_harness_started_is_followed_across_a_restart(tmp_path):
    first, second = sleeper(), sleeper()
    current = {"pid": first.pid}
    try:
        w = machine.MachineWatcher(make_log(tmp_path), server_pid_fn=lambda: current["pid"])
        assert w.sample(0.0)["server"]["pid"] == first.pid
        current["pid"] = second.pid                        # E2: the CSMS was restarted
        assert w.sample(0.0)["server"]["pid"] == second.pid
    finally:
        for c in (first, second):
            c.kill()
            c.wait()


def test_the_server_is_found_automatically(tmp_path):
    child = sleeper("-m", "csms.server")                  # argv carries the module argument
    try:
        time.sleep(0.3)
        w = machine.MachineWatcher(make_log(tmp_path))
        server = w.sample(0.0)["server"]
        assert server is not None and server["pid"] == child.pid
    finally:
        child.kill()
        child.wait()


def test_a_missing_server_is_null_not_an_error(tmp_path):
    w = machine.MachineWatcher(make_log(tmp_path), server_pid=2 ** 22 + 12345)
    assert w.sample(0.0)["server"] is None


def test_without_psutil_the_message_says_how_to_fix_it(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)
    with pytest.raises(SystemExit, match="pip install -r requirements.txt"):
        machine.load_psutil()


@pytest.mark.asyncio
async def test_the_watcher_writes_one_line_per_interval(tmp_path):
    log = make_log(tmp_path)
    w = machine.MachineWatcher(log, interval_s=0.05)
    task = asyncio.ensure_future(w.run())
    await asyncio.sleep(0.4)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    log.close()
    rows = [r for r in TimingLog.read(tmp_path / "m.jsonl") if r["event_type"] == tl.MACHINE_SAMPLE]
    assert len(rows) >= 3 and w.samples == len(rows)
    assert all(r["station_id"] is None and r["loop_lag_ms"] >= 0.0 for r in rows)


# -- the load generator ------------------------------------------------------------------


def test_the_flags_parse():
    ns = lg.build_parser().parse_args(["--n", "5", "--watch-machine", "--server-pid", "42"])
    assert ns.watch_machine is True and ns.server_pid == 42 and ns.watch_machine_every == 1.0
    ns = lg.build_parser().parse_args(["--n", "5"])
    assert ns.watch_machine is False and ns.server_pid is None


@pytest.mark.asyncio
async def test_a_real_fleet_run_records_the_machine(tmp_path):
    args = lg.build_parser().parse_args([
        "--n", "3", "--experiment", "s1", "--csms-url", "ws://localhost:9292",
        "--charge-for", "1.5", "--meter-every", "0.2", "--stagger", "0.01",
        "--log-level", "WARNING", "--reconnect-max-attempts", "1", "--no-analyse",
        "--timing-log", str(tmp_path / "s1_n3_classical.jsonl"),
        "--watch-machine", "--watch-machine-every", "0.2",
    ])
    config = lg.AgentConfig.from_namespace(args)
    async with FakeCSMS(port=9292):
        code = await lg.main_async(args, config)
    assert code == 0
    rows = TimingLog.read(tmp_path / "s1_n3_classical.jsonl")
    samples = [r for r in rows if r["event_type"] == tl.MACHINE_SAMPLE]
    assert len(samples) >= 3
    assert rows[0]["event_type"] == tl.RUN_STARTED and rows[0]["watch_machine"] is True


# -- the analysis --------------------------------------------------------------------------


def add_samples(d: Diaries, info: dict, *, n: int, cpu: float, lag: float,
                server: bool = True, mode: str = "classical", experiment: str = "s1") -> None:
    H = dict(run_id="run1", experiment=experiment, n=n, mode=mode, tls=True)
    for k in range(20):
        d.harness(info["name"], T0 + 0.5 + k * 0.3, "machine_sample", cpu_pct=cpu + (k % 3),
                  cpu_count=8, mem_pct=40.0, mem_used_mb=6000.0, loop_lag_ms=lag + (k % 2),
                  tester={"pid": 1, "cpu_pct": 30.0 + k, "rss_mb": 100.0 + k, "threads": 4},
                  server={"pid": 2, "cpu_pct": 20.0, "rss_mb": 80.0, "threads": 6} if server else None,
                  **H)


def _matched(d: Diaries, name: str):
    events, logs = d.write()
    return match.match_run(collect.read_harness_file(logs / name).runs[0],
                           collect.read_server_diary(events))


def test_machine_readings_are_summarised(tmp_path):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="s1", n=3, mode="classical")
    add_samples(d, info, n=3, cpu=40.0, lag=2.0)
    m = mm.measure(_matched(d, info["name"]))
    assert m["samples"] == 20 and m["cpu_count"] == 8
    assert m["cpu_pct"]["max"] == 42.0 and m["loop_lag_ms"]["max"] == 3.0
    assert m["tester"]["rss_mb_max"] == 119.0 and m["server"]["found"] is True
    assert m["saturated"] is False and m["saturation_reasons"] == []
    assert len(m["timeline"]["cpu_pct"]) == 20


def test_no_samples_means_no_machine_section_and_no_warning(tmp_path):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="s1", n=3, mode="classical")
    m = _matched(d, info["name"])
    assert mm.measure(m) is None
    slot = run.analyse_run(m)
    assert slot["machine"] is None
    assert "machine_saturated" not in {i["code"] for i in slot["trust"]["issues"]}


@pytest.mark.parametrize("cpu,lag,reason", [
    (95.0, 2.0, "whole-machine CPU p95"),
    (40.0, 120.0, "event-loop lag p95"),
])
def test_a_saturated_laptop_is_a_trust_warning(tmp_path, cpu, lag, reason):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="s1", n=3, mode="classical")
    add_samples(d, info, n=3, cpu=cpu, lag=lag)
    slot = run.analyse_run(_matched(d, info["name"]))
    assert slot["machine"]["saturated"] is True
    issue = next(i for i in slot["trust"]["issues"] if i["code"] == "machine_saturated")
    assert issue["level"] == "warn" and reason in issue["message"]


def test_the_server_not_found_is_reported_plainly(tmp_path):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="s1", n=3, mode="classical")
    add_samples(d, info, n=3, cpu=40.0, lag=2.0, server=False)
    m = mm.measure(_matched(d, info["name"]))
    assert m["server"] == {"found": False, "samples": 0, "cpu_pct": {"n": 0},
                           "rss_mb_max": None, "threads_max": None}


def test_s1_series_against_fleet_size(tmp_path):
    slots = []
    for n, cpu in ((3, 20.0), (5, 50.0), (8, 90.0)):
        d = Diaries(tmp_path / f"n{n}")
        info = d.fleet_run(experiment="s1", n=n, mode="classical")
        add_samples(d, info, n=n, cpu=cpu, lag=5.0)
        slots.append(run.analyse_run(_matched(d, info["name"])))
    rows = compare.measure(slots)["machine_vs_n"]["s1 · classical · TLS"]
    assert [r["n"] for r in rows] == [3, 5, 8]
    assert [r["saturated"] for r in rows] == [False, False, True]
    assert rows[0]["server_cpu_p95"] == 20.0


def test_the_analysis_does_not_need_psutil():
    src = open(mm.__file__, encoding="utf-8").read()
    assert "psutil" not in src.replace("psutil's", "")
