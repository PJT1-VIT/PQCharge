"""
Reconnection tests — the CSMS dies, the station survives, the session
continues.

Track C (tests). Phase C4.

--------------------------------------------------------------------
WHAT THESE PROVE

E2 kills the CSMS and times how long the fleet takes to recover. For
that number to mean anything, one station has to get the outage right
first:

  * it retries instead of giving up
  * it waits a JITTERED delay between attempts
  * it keeps charging while the server is away, because the car does
  * it queues what it could not report, and replays it with the
    original timestamps
  * it resumes the SAME transaction, with seq_no unbroken
  * it does not charge for longer just because the server went away

Each test starts a real fake CSMS, takes it down mid-session, brings it
back, and asserts on what the server received afterwards.

--------------------------------------------------------------------
PORTS

9250+, clear of the real CSMS (9000), the fixture default (9100),
test_client.py (9210-9229) and test_actuation.py (9230-9249).
"""

from __future__ import annotations

import asyncio
import time

import pytest

from agent import messages as msg
from agent.config import AgentConfig
from agent.state_machine import StationState
from agent.station import ChargingStation
from tests.fixtures.fake_csms import FakeCSMS

pytestmark = pytest.mark.asyncio


def make_config(port: int, **overrides) -> AgentConfig:
    """
    A config pointed at a test server, with the backoff compressed.

    base_delay 0.05s rather than 1s: the arithmetic is tested properly
    in test_backoff.py with no clock at all, so these tests only need
    the loop to turn over quickly. max_attempts stays 0 (forever) except
    where a test is specifically about giving up.
    """
    defaults = dict(
        station_id="CP001",
        csms_url=f"ws://localhost:{port}",
        charge_for_s=3.0,
        meter_every_s=0.1,
        log_level="WARNING",
        reconnect_base_delay_s=0.05,
        reconnect_max_delay_s=0.2,
        reconnect_jitter=1.0,
    )
    defaults.update(overrides)
    return AgentConfig(**defaults)


async def _outage_after(server: FakeCSMS, delay: float, length: float) -> None:
    """Take the server down mid-session and bring it back."""
    await asyncio.sleep(delay)
    await server.outage(length)


def events_of(server: FakeCSMS):
    """The handler for the connection that arrived AFTER the outage.

    come_back() clears the history, so connections[0] is the post-outage
    one — which is what a replay assertion wants to look at.
    """
    return server.connections[0]


# =====================================================================
# THE CORE PATH
# =====================================================================


async def test_the_station_reconnects_after_an_outage():
    """
    THE test in this file. Server dies mid-charge, comes back, station
    rejoins and finishes its session.
    """
    async with FakeCSMS(port=9250) as server:
        station = ChargingStation(make_config(9250))
        outage = asyncio.ensure_future(_outage_after(server, 0.6, 0.5))
        ok = await station.run()
        await outage

    assert ok is True, "the station never completed its session"
    assert station.reconnections >= 1
    assert station.connection_attempts >= 2
    assert station.total_downtime_s > 0


async def test_the_transaction_survives_the_outage():
    """
    The same transaction id on both sides of the gap.

    A station that started a NEW transaction after reconnecting would
    look plausible — events still flow, energy still climbs — and would
    silently split one charging session into two in Track A's log,
    breaking every per-session figure at Stage 9.
    """
    async with FakeCSMS(port=9251) as server:
        station = ChargingStation(make_config(9251))
        before = None

        async def watch():
            nonlocal before
            await asyncio.sleep(0.6)
            before = station.transaction_id
            await server.outage(0.5)

        watcher = asyncio.ensure_future(watch())
        await station.run()
        await watcher

    assert before is not None
    assert events_of(server).last_transaction_id == before


async def test_the_station_keeps_charging_while_the_server_is_away():
    """
    *** The car does not care that a server went away. ***

    A real charger does not dump the driver's session because it lost
    its uplink. If the contactor opened here, every E2 run would cut
    power to five hundred cars and the energy curve would show the
    outage as a genuine loss of supply rather than a loss of reporting.
    """
    seen_closed: list[bool] = []

    async with FakeCSMS(port=9252) as server:
        station = ChargingStation(make_config(9252))

        async def watch():
            await asyncio.sleep(0.6)
            await server.go_down()
            await asyncio.sleep(0.3)
            # Mid-outage: nothing is listening, and current is flowing.
            seen_closed.append(station.power.is_closed())
            seen_closed.append(station.state.state is StationState.CHARGING)
            await server.come_back()

        watcher = asyncio.ensure_future(watch())
        await station.run()
        await watcher

    assert seen_closed == [True, True], "the station stopped charging during the outage"
    # And it is safely open once the station itself finishes.
    assert station.power.is_closed() is False


async def test_readings_taken_during_the_outage_are_replayed():
    """
    Throwing them away would put a hole in the energy curve exactly as
    wide as the outage — and at Stage 9 that hole is indistinguishable
    from a station that genuinely stopped charging.
    """
    async with FakeCSMS(port=9253) as server:
        station = ChargingStation(make_config(9253))
        outage = asyncio.ensure_future(_outage_after(server, 0.6, 0.6))
        await station.run()
        await outage

    assert station.offline_queue.replayed_total > 0, "nothing was queued at all"
    assert station.offline_queue.dropped_total == 0


async def test_replayed_events_carry_their_original_timestamps():
    """
    Load-bearing. csms/registry.py's last_meter_at guard uses the
    timestamp to refuse letting a replayed reading overwrite newer live
    state — Track A's note says aggregate_power_w "would jump backwards
    on the dashboard at exactly the moment the fleet is being watched
    recover".

    If replays were stamped now(), every stale event would look newest,
    the guard would never fire, and Track A's defence against this exact
    problem would be silently disabled by us. Nothing would error.
    """
    async with FakeCSMS(port=9254) as server:
        station = ChargingStation(make_config(9254))
        outage = asyncio.ensure_future(_outage_after(server, 0.6, 0.6))
        await station.run()
        await outage

    stamps = events_of(server).event_timestamps
    assert len(stamps) >= 3
    # The replayed block arrives first on the new connection and its
    # timestamps predate the live events that follow it.
    assert stamps == sorted(stamps), (
        "timestamps are not in chronological order — replays were probably "
        "stamped at send time instead of at reading time"
    )
    assert stamps[0] < stamps[-1]


async def test_replayed_events_are_marked_offline():
    """
    Track A records the flag, and analysis uses it to measure how much
    replay occurred instead of guessing. Sending replays unmarked would
    make an outage invisible in their log.
    """
    async with FakeCSMS(port=9255) as server:
        station = ChargingStation(make_config(9255))
        outage = asyncio.ensure_future(_outage_after(server, 0.6, 0.6))
        await station.run()
        await outage

    flags = events_of(server).offline_flags
    assert True in flags, "no event arrived marked offline=True"
    assert False in flags, "every event was marked offline — live ones should not be"


async def test_sequence_numbers_are_unbroken_across_the_outage():
    """
    Safety rule 2, across a reconnection.

    Track A detects forward jumps in seq_no and logs them as evidence of
    message loss during a storm. That is a real finding worth having —
    and a jump caused by our own counter resetting, or by replay
    arriving out of order, would be a FABRICATED one, indistinguishable
    at analysis from genuine loss.
    """
    async with FakeCSMS(port=9256) as server:
        station = ChargingStation(make_config(9256))
        outage = asyncio.ensure_future(_outage_after(server, 0.6, 0.5))
        await station.run()
        await outage

    seqs = events_of(server).seq_numbers
    assert seqs == sorted(seqs), f"events arrived out of order: {seqs}"
    assert len(seqs) == len(set(seqs)), f"an event was replayed twice: {seqs}"
    # Contiguous: no gaps within what this connection received.
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), seqs


async def test_the_connector_status_is_re_announced_after_reconnecting():
    """
    The server has never seen a StatusNotification on the new
    connection: csms/registry.py builds a fresh StationSession with
    ocpp_status=None. A station that suppressed the resend because it
    remembered sending the same value on the OLD socket would be
    charging normally while the dashboard showed a blank connector for
    the rest of the run.
    """
    async with FakeCSMS(port=9257) as server:
        station = ChargingStation(make_config(9257))
        outage = asyncio.ensure_future(_outage_after(server, 0.6, 0.4))
        await station.run()
        await outage

    assert msg.STATUS_OCCUPIED in events_of(server).statuses


async def test_the_session_does_not_get_longer_because_of_the_outage():
    """
    The deadline is set when the transaction opens and is not reset on
    reconnect. Otherwise every interrupted session would charge for
    extra time proportional to the outage, putting the outage length
    into the energy totals E5 compares across runs.
    """
    async with FakeCSMS(port=9258) as server:
        station = ChargingStation(make_config(9258, charge_for_s=1.0))
        started = asyncio.get_running_loop().time()
        outage = asyncio.ensure_future(_outage_after(server, 0.3, 0.5))
        await station.run()
        elapsed = asyncio.get_running_loop().time() - started
        await outage

    # 1.0s of charging; the outage overlaps it rather than extending it.
    # Generous ceiling because reconnect and replay take real time.
    assert elapsed < 2.5, f"the session ran {elapsed:.2f}s, far past its deadline"


async def test_an_outage_longer_than_the_session_closes_it_on_reconnect():
    """
    The deadline passes while nobody is listening. The transaction must
    still be closed properly — with its queued readings replayed first,
    so the energy total is complete even though the last stretch was
    reported late.
    """
    async with FakeCSMS(port=9259) as server:
        station = ChargingStation(make_config(9259, charge_for_s=0.8))
        outage = asyncio.ensure_future(_outage_after(server, 0.4, 1.2))
        ok = await station.run()
        await outage

    assert ok is True
    assert station.transaction_id is None
    assert msg.TX_ENDED in events_of(server).event_types
    assert station.power.is_closed() is False


# =====================================================================
# GIVING UP, AND NOT GIVING UP
# =====================================================================


async def test_a_server_that_never_comes_back_is_survived_not_crashed():
    """
    max_attempts bounds it. Without a limit this would run forever,
    which is correct for E2 and useless for a test.
    """
    station = ChargingStation(
        make_config(9260, reconnect_max_attempts=3, charge_for_s=0.2)
    )
    ok = await station.run()

    assert ok is False
    assert station.connection_attempts == 3
    assert station.power.is_closed() is False


async def test_a_refused_boot_stops_the_station_instead_of_retrying():
    """
    The server ANSWERED, and the answer was no. Retrying would have the
    station hammer a CSMS that has already refused it — and from Stage 6
    that refusal is meaningful, marking a station whose capabilities
    exclude the migration target.
    """
    from tests.fixtures.fake_csms import FaultConfig

    async with FakeCSMS(port=9261, faults=FaultConfig(fail_boot=True)):
        station = ChargingStation(make_config(9261))
        ok = await station.run()

    assert ok is False
    assert station.connection_attempts == 1, "a refused boot was retried"


async def test_a_blocked_card_stops_the_station_instead_of_retrying():
    """
    Same principle one step later: the card was refused, so this
    station's work is done. Without the guard, one blocked card becomes
    a station that reconnects and re-asks for the whole length of a run.
    """
    async with FakeCSMS(port=9262) as server:
        station = ChargingStation(make_config(9262, id_token="TAG-BLOCKED"))
        ok = await station.run()

    assert ok is True
    assert station.connection_attempts == 1
    assert station.transaction_id is None


async def test_a_dropped_connection_is_not_the_same_as_an_outage():
    """
    --drop-after closes one socket while the server stays up: the agent
    reconnects on its FIRST attempt and never really backs off. Worth
    proving separately, because a test that only covered the full outage
    would not notice the fast path breaking.
    """
    from tests.fixtures.fake_csms import FaultConfig

    async with FakeCSMS(port=9263, faults=FaultConfig(drop_after=6)) as server:
        station = ChargingStation(
            make_config(9263, charge_for_s=0.8, reconnect_max_attempts=4)
        )
        await station.run()

    assert station.connection_attempts >= 2
    assert len(server.connections) >= 2, "the station never came back"


# =====================================================================
# THE QUEUE UNDER PRESSURE
# =====================================================================


async def test_a_full_queue_reports_its_gaps_rather_than_hiding_them():
    """
    A run with dropped events is still usable. A run with dropped events
    nobody knew about is a dataset with an unexplained hole discovered
    at Stage 9, on a file that cannot be regenerated because the run is
    over.

    --------------------------------------------------------------
    WHY THIS DRIVES _wait_offline DIRECTLY INSTEAD OF STAGING AN OUTAGE

    It used to run a real outage and assert that the queue overflowed.
    That version passed on Linux and failed on Windows, and the reason
    was worth keeping:

        reconnecting in 0.03s (attempt 2, ceiling 0.05s, full)
        *** RECOVERED ***

    One backoff wait, and then the connect() attempt to the dead port
    blocked for the REST of the outage instead of being refused. On
    Linux that refusal is instant. So the station only ever got one
    0.03s metering window and queued almost nothing -- nothing to drop.

    That is a genuine platform difference, not a flaky test, and it is
    why CONNECT_TIMEOUT_S now exists in station.py. But it makes "how
    many readings fit inside an outage" a property of the operating
    system's connect behaviour, which is no basis for an assertion.

    So the outage is simulated by calling the offline metering loop for
    a known length of time. What is being tested -- does a full queue
    drop the oldest and say so -- is exercised exactly as it is in
    production. That readings are queued during a REAL outage is
    covered by test_readings_taken_during_the_outage_are_replayed.
    """
    from agent.offline_queue import OfflineQueue

    station = ChargingStation(make_config(9264, meter_every_s=0.05))
    station.offline_queue = OfflineQueue(max_events=2, station_id="CP001")

    # Put the station where an outage would find it: plugged in,
    # charging, contactor closed, with plenty of time left on the clock.
    station.transaction_id = "tx-overflow-test"
    station.state.transition_to(StationState.OCCUPIED, "test setup")
    station.state.transition_to(StationState.CHARGING, "test setup")
    station._apply_power_for_state()
    station._charge_deadline = time.monotonic() + 30.0

    # ~8 meter ticks at 0.05s into a queue that holds 2.
    await station._wait_offline(0.4)

    assert station.offline_queue.queued_total >= 4, (
        "the offline meter barely ran; the timing in this test is wrong"
    )
    assert station.offline_queue.dropped_total > 0
    assert len(station.offline_queue) == 2
    assert "GAPS" in station.offline_queue.describe()

    # The survivors are the readings NEAREST the reconnection, in order,
    # which is what makes a truncated replay still useful.
    survivors = station.offline_queue.drain()
    assert [e.seq_no for e in survivors] == sorted(e.seq_no for e in survivors)
    assert survivors[-1].seq_no == station.seq_no - 1


async def test_nothing_is_queued_when_no_transaction_is_open():
    """
    An outage between sessions has nothing to record. Queuing empty
    readings would put phantom events into the log for a station that
    was idle.
    """
    async with FakeCSMS(port=9265) as server:
        station = ChargingStation(make_config(9265, id_token="TAG-BLOCKED"))
        await station.run()

    assert station.offline_queue.queued_total == 0


# =====================================================================
# CANCELLATION — the harness shutting a station down (C5 depends on this)
# =====================================================================


async def test_cancelling_during_an_outage_opens_the_contactor():
    """
    C5 creates five hundred of these and cancels them at the end of a
    run. A station cancelled MID-OUTAGE is the awkward case: it is
    charging, it has no connection, and the conditional safety rule is
    deliberately leaving the contactor closed. run()'s finally is what
    guarantees it still opens.

    On simulated hardware this is a number. On the Raspberry Pi bench
    node it is a relay with current behind it.
    """
    async with FakeCSMS(port=9266) as server:
        station = ChargingStation(make_config(9266, charge_for_s=30.0))
        task = asyncio.ensure_future(station.run())

        await asyncio.sleep(0.6)
        await server.go_down()
        await asyncio.sleep(0.2)

        assert station.power.is_closed() is True  # still charging, as it should be

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert station.power.is_closed() is False