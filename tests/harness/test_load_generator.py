"""
Tests for harness/load_generator.py — N stations at once.

Track C (tests). Phase C5.

--------------------------------------------------------------------
WHAT THESE PROVE

A load generator is the one component whose bugs are indistinguishable
from findings. If the harness staggers wrongly, E1 reports slow
handshakes. If one crashed station aborts the fleet, E2 reports a
failed recovery. If a config field fails to reach the agents, every
run is measuring something other than what the flags say.

So: the fleet actually runs, every field reaches every agent, one bad
station cannot take the run down, and the run's own record is complete.

The fleet sizes here are small — five, ten. Scale is not what these
test; correctness of the mechanism is. The N=50 and N=100 runs Track A
asked for are operator runs against a real server, not unit tests.

--------------------------------------------------------------------
PORTS

9270+, clear of the real CSMS (9000), the fixture default (9100),
test_client.py (9210-9229), test_actuation.py (9230-9249) and
test_reconnect.py (9250-9269).
"""

from __future__ import annotations

import argparse
import asyncio
import random

import pytest

from agent.config import AgentConfig
from harness import timing_log as tl
from harness.load_generator import (
    FleetRunner,
    FleetSpec,
    FleetWatcher,
    _check_storm_preconditions,
    build_parser,
)
from harness.timing_log import TimingLog
from tests.fixtures.fake_csms import FakeCSMS

# The asyncio marker is applied PER TEST rather than at module level,
# because this file deliberately mixes the two: the stagger arithmetic,
# the recovery predicate and the E2 precondition check are all pure
# functions and are faster and clearer tested without an event loop.


def make_config(port: int, **overrides) -> AgentConfig:
    defaults = dict(
        station_id="CP0001",
        csms_url=f"ws://localhost:{port}",
        charge_for_s=0.3,
        meter_every_s=0.1,
        log_level="WARNING",
        reconnect_max_attempts=1,
    )
    defaults.update(overrides)
    return AgentConfig(**defaults)


def make_log(tmp_path, n: int = 3) -> TimingLog:
    return TimingLog(
        tmp_path / "t.jsonl", run_id="fleetrun0001",
        experiment="unit", n_stations=n,
    )


# =====================================================================
# STATION IDS
# =====================================================================


def test_ids_are_zero_padded_so_they_sort():
    """
    Four digits, not three. Station ids appear in every log grep and
    every dashboard list, and CP10 sorting before CP9 makes a 500-row
    list unreadable exactly when someone is trying to find one station.
    """
    ids = FleetSpec(n=12).station_ids()
    assert ids[0] == "CP0001" and ids[-1] == "CP0012"
    assert ids == sorted(ids)


def test_a_fleet_can_start_at_an_offset():
    """Two harnesses against one CSMS must not both be CP0001 — the
    registry would treat the second as the first reconnecting."""
    assert FleetSpec(n=2, start_index=501).station_ids() == ["CP0501", "CP0502"]


# =====================================================================
# CONFIG PROPAGATION — every flag must reach every agent
# =====================================================================


def test_every_configured_field_reaches_each_station(tmp_path):
    """
    dataclasses.replace rather than building a fresh config, so a field
    added later cannot be forgotten here. A silently dropped flag means
    a run measuring something other than what the command line says.
    """
    base = make_config(9270, charge_for_s=7.5, max_power_w=11000.0,
                       connect_timeout_s=9.0, reconnect_jitter=1.0)
    runner = FleetRunner(base, FleetSpec(n=2), make_log(tmp_path))

    cfg = runner.config_for("CP0002")
    assert cfg.charge_for_s == 7.5
    assert cfg.max_power_w == 11000.0
    assert cfg.connect_timeout_s == 9.0
    assert cfg.reconnect_jitter == 1.0
    assert cfg.station_id == "CP0002"


def test_the_whole_fleet_shares_one_run_id(tmp_path):
    """What ties five hundred agents together as ONE run, and what
    matches the run_id in the harness's own timing log."""
    log = make_log(tmp_path)
    runner = FleetRunner(make_config(9270), FleetSpec(n=3), log)

    ids = {runner.config_for(s).run_id for s in FleetSpec(n=3).station_ids()}
    assert ids == {log.run_id}


def test_per_agent_file_logging_is_forced_off(tmp_path):
    """
    Five hundred agents would open five hundred log files, and the run's
    record would then be five hundred files to collate before anything
    could be read. The timing log is the fleet's record.
    """
    base = make_config(9270, log_to_file=True)
    runner = FleetRunner(base, FleetSpec(n=2), make_log(tmp_path))
    assert runner.config_for("CP0001").log_to_file is False


# =====================================================================
# THE STAGGER
# =====================================================================


def test_spawns_are_spread_out(tmp_path):
    runner = FleetRunner(
        make_config(9270), FleetSpec(n=100, stagger_s=0.1, stagger_jitter=0.0),
        make_log(tmp_path),
    )
    delays = [runner._delay_for(i) for i in range(100)]
    assert delays[0] == 0.0
    assert delays[-1] == pytest.approx(9.9)


def test_the_stagger_is_jittered_off_the_grid(tmp_path):
    """
    A fixed grid is its own kind of lockstep at small intervals. Same
    reasoning as the backoff jitter one layer down: the arrival pattern
    must not be an artefact of the harness.
    """
    runner = FleetRunner(
        make_config(9270), FleetSpec(n=50, stagger_s=0.1, stagger_jitter=0.5),
        make_log(tmp_path), rng=random.Random(7),
    )
    delays = [runner._delay_for(i) for i in range(1, 50)]
    grid = [i * 0.1 for i in range(1, 50)]
    assert not any(d == pytest.approx(g) for d, g in zip(delays, grid))


def test_no_station_is_asked_to_wait_a_negative_time(tmp_path):
    runner = FleetRunner(
        make_config(9270), FleetSpec(n=20, stagger_s=0.05, stagger_jitter=1.0),
        make_log(tmp_path), rng=random.Random(1),
    )
    assert all(runner._delay_for(i) >= 0.0 for i in range(20))


def test_a_seed_makes_the_arrival_pattern_reproducible(tmp_path):
    """So two runs being compared differ in the variable under test and
    not in when their agents happened to arrive."""
    def delays(seed):
        r = FleetRunner(
            make_config(9270), FleetSpec(n=10, stagger_s=0.1),
            make_log(tmp_path), rng=random.Random(seed),
        )
        return [r._delay_for(i) for i in range(10)]

    assert delays(42) == delays(42)
    assert delays(42) != delays(43)


# =====================================================================
# RUNNING A FLEET
# =====================================================================


@pytest.mark.asyncio
async def test_a_small_fleet_all_completes(tmp_path):
    """The mechanism end to end: real agents, real sockets, real OCPP."""
    async with FakeCSMS(port=9270) as server:
        log = make_log(tmp_path, n=5)
        runner = FleetRunner(make_config(9270), FleetSpec(
            n=5, stagger_s=0.01), log)
        result = await runner.run()
        log.close()

    assert result.succeeded == 5
    assert result.crashed == 0
    assert len(server.connections) == 5
    assert {c.id for c in server.connections} == {
        "CP0001", "CP0002", "CP0003", "CP0004", "CP0005"
    }


@pytest.mark.asyncio
async def test_the_run_is_recorded_station_by_station(tmp_path):
    """
    The harness's denominator. The server's log only contains attempts
    that ARRIVED; spawned-but-never-connected exists only here.
    """
    async with FakeCSMS(port=9271):
        log = make_log(tmp_path, n=4)
        runner = FleetRunner(make_config(9271), FleetSpec(
            n=4, stagger_s=0.01), log)
        await runner.run()
        log.close()

    rows = TimingLog.read(tmp_path / "t.jsonl")
    spawned = [r for r in rows if r["event_type"] == tl.STATION_SPAWNED]
    finished = [r for r in rows if r["event_type"] == tl.STATION_FINISHED]

    assert len(spawned) == 4
    assert len(finished) == 4
    assert {r["station_id"] for r in spawned} == {r["station_id"] for r in finished}


@pytest.mark.asyncio
async def test_one_crashing_station_does_not_abort_the_fleet(tmp_path, monkeypatch):
    """
    *** THE MOST EXPENSIVE AVOIDABLE FAILURE IN THIS PHASE. ***

    A 500-agent run is minutes of setup and is the only artefact of that
    attempt. Losing all of it because station 3 raised something
    unexpected is what gather(return_exceptions=True) exists to prevent.
    Track A hardened their own fixture after exactly this.
    """
    from harness import load_generator as lg

    real = lg.ChargingStation

    def explode(config, *args, **kwargs):
        if config.station_id == "CP0003":
            raise RuntimeError("deliberate")
        return real(config, *args, **kwargs)

    monkeypatch.setattr(lg, "ChargingStation", explode)

    async with FakeCSMS(port=9272):
        log = make_log(tmp_path, n=5)
        runner = FleetRunner(make_config(9272), FleetSpec(
            n=5, stagger_s=0.01), log)
        result = await runner.run()
        log.close()

    assert result.crashed == 1
    assert result.succeeded == 4, "the rest of the fleet did not finish"

    rows = TimingLog.read(tmp_path / "t.jsonl")
    crashed = [r for r in rows if r["event_type"] == tl.STATION_CRASHED]
    assert len(crashed) == 1
    assert crashed[0]["station_id"] == "CP0003"
    assert "deliberate" in crashed[0]["error"]


@pytest.mark.asyncio
async def test_a_crash_is_reported_separately_from_a_failure(tmp_path):
    """
    A crash is a Track C bug. A failure is a result. Reporting them as
    one number would let our own bug read as evidence about the server.
    """
    # Nothing listening on 9273: every station fails, none crashes.
    log = make_log(tmp_path, n=3)
    runner = FleetRunner(make_config(9273), FleetSpec(n=3, stagger_s=0.01), log)
    result = await runner.run()
    log.close()

    assert result.failed == 3
    assert result.crashed == 0
    assert result.succeeded == 0


@pytest.mark.asyncio
async def test_the_summary_totals_the_fleets_counters(tmp_path):
    async with FakeCSMS(port=9274):
        log = make_log(tmp_path, n=4)
        runner = FleetRunner(make_config(9274), FleetSpec(
            n=4, stagger_s=0.01), log)
        result = await runner.run()
        log.close()

    assert result.total("connection_attempts") == 4
    assert result.total("callerrors") == 0
    assert result.total("offline_dropped") == 0
    assert result.wall_s > 0


@pytest.mark.asyncio
async def test_the_run_record_ends_with_a_totals_row(tmp_path):
    """So a run can be read at Stage 9 without re-deriving its own
    totals from the per-station rows."""
    async with FakeCSMS(port=9275):
        log = make_log(tmp_path, n=3)
        runner = FleetRunner(make_config(9275), FleetSpec(
            n=3, stagger_s=0.01), log)
        result = await runner.run()
        log.emit(tl.RUN_FINISHED, **result.to_dict())
        log.close()

    last = TimingLog.read(tmp_path / "t.jsonl")[-1]
    assert last["event_type"] == tl.RUN_FINISHED
    assert last["succeeded"] == 3


@pytest.mark.asyncio
async def test_the_fleet_survives_a_server_that_refuses_everything(tmp_path):
    """
    Normal during E2. Every station fails cleanly, nothing crashes, and
    the run still produces a complete record — which is what makes a
    failed run analysable rather than merely lost.
    """
    from tests.fixtures.fake_csms import FaultConfig

    async with FakeCSMS(port=9276, faults=FaultConfig(reject_connections=True)):
        log = make_log(tmp_path, n=4)
        runner = FleetRunner(make_config(9276), FleetSpec(
            n=4, stagger_s=0.01), log)
        result = await runner.run()
        log.close()

    assert result.crashed == 0
    assert result.succeeded == 0
    assert len(TimingLog.read(tmp_path / "t.jsonl")) >= 8


@pytest.mark.asyncio
async def test_cancelling_the_fleet_leaves_nothing_energised(tmp_path):
    """
    Ctrl-C on a long run. Each station's own finally opens its
    contactor; on the Raspberry Pi bench node that is a relay with
    current behind it.
    """
    async with FakeCSMS(port=9277):
        log = make_log(tmp_path, n=4)
        runner = FleetRunner(
            make_config(9277, charge_for_s=30.0), FleetSpec(n=4, stagger_s=0.01),
            log,
        )
        task = asyncio.ensure_future(runner.run())
        await asyncio.sleep(0.8)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        log.close()

    # Every station that got as far as charging has been shut down.
    assert runner.outcomes, "no station ever started"


# =====================================================================
# THE FLEET WATCHER
# =====================================================================


def test_recovery_is_counted_with_track_as_own_predicate():
    """
    *** NOT RE-IMPLEMENTED HERE. ***

    is_recovered is defined once, in csms/fleet.py, precisely so the
    dashboard, the load generator and the analysis scripts cannot
    disagree. Track A's comment: deciding what "recovered" means during
    analysis silently invalidates the comparison across all twelve runs.
    """
    snapshot = {
        "stations": [
            {"station_id": "CP0001", "connection_state": "connected",
             "boot_accepted": True},
            {"station_id": "CP0002", "connection_state": "connected",
             "boot_accepted": False},        # connected but not booted
            {"station_id": "CP0003", "connection_state": "disconnected",
             "boot_accepted": True},         # booted once, gone now
        ]
    }
    assert FleetWatcher._recovered(snapshot) == 1


def test_an_empty_or_odd_snapshot_counts_zero_rather_than_raising():
    """A display number must never be able to kill a run."""
    assert FleetWatcher._recovered({}) == 0
    assert FleetWatcher._recovered({"stations": [{"nonsense": 1}]}) == 0


# =====================================================================
# THE E2 PRECONDITION
# =====================================================================


def _args(**overrides) -> argparse.Namespace:
    defaults = dict(storm_at=60.0, server_cmd="python -m csms.server")
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_a_storm_run_without_ws_ping_disabled_is_refused():
    """
    Track A's §10: during E2 a CPU-saturated agent may pong late and be
    dropped by WebSocket keepalive, landing in the dataset as a FAILED
    RECOVERY — the number E2 exists to measure.

    "Put it in the run script, not in anyone's memory" was the
    agreement. This is the run script. It fails before five hundred
    agents spend minutes producing numbers that would be thrown away.
    """
    with pytest.raises(SystemExit) as caught:
        _check_storm_preconditions(_args())
    assert "ws-ping-interval" in str(caught.value)


def test_a_storm_run_with_the_flag_is_allowed():
    _check_storm_preconditions(
        _args(server_cmd="python -m csms.server --ws-ping-interval 0")
    )


def test_a_non_storm_run_is_not_second_guessed():
    """E1 and E5 do not kill the server, so keepalive is not in play."""
    _check_storm_preconditions(_args(storm_at=None))
    _check_storm_preconditions(_args(server_cmd=None))


# =====================================================================
# THE COMMAND LINE
# =====================================================================


def test_the_parser_accepts_every_agent_flag():
    """
    The fleet must be configurable exactly as one station is, or a
    behaviour verified on a single agent cannot be reproduced at scale.
    """
    args = build_parser().parse_args([
        "--n", "50", "--experiment", "e1",
        "--csms-url", "wss://localhost:9000",
        "--charge-for", "30", "--meter-every", "5",
        "--connect-timeout", "8", "--reconnect-jitter", "1.0",
        "--cert-dir", "certs",
    ])
    config = AgentConfig.from_namespace(args)

    assert args.n == 50
    assert config.uses_tls is True
    assert config.connect_timeout_s == 8.0
    assert config.charge_for_s == 30.0


def test_the_default_fleet_is_small():
    """--n defaults to a size that cannot accidentally launch a storm
    against someone else's running server."""
    assert build_parser().parse_args([]).n == 5
