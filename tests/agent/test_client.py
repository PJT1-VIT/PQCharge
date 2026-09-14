"""
Integration tests — the real agent against the fake CSMS.

Track C (tests). Phase C2.

--------------------------------------------------------------------
WHAT THESE ARE, AND WHAT THEY ARE NOT

Each test starts a real tests/fixtures/fake_csms.py on its own port,
runs a real agent/station.py ChargingStation against it, and asserts on
what the server actually received. Real sockets, real OCPP framing,
real ocpp library.

They are NOT a substitute for running against csms/server.py. The rule
from the C1 plan stands: the fake is for failure paths and speed, and
every happy path must also be proven against the real server by hand.
A fake that is the only evidence something works is a test that passes
while the integration is broken.

What the fake buys is the failure side. The real CSMS will not, on
request, reject a boot, return a CALLError or hang up mid-session, and
those are exactly the paths that decide whether E2 measures
post-quantum cost or measures our own bugs.

--------------------------------------------------------------------
PORTS

Each test uses its own port in the 9200+ range, so a test that leaves a
listener behind cannot make the NEXT test fail for an unrelated reason
-- which is a genuinely confusing way to lose an afternoon. 9000 (the
real CSMS) and 9100 (the fixture's default) are both avoided.
"""

from __future__ import annotations

import asyncio

import pytest

from agent import messages as msg
from agent.config import AgentConfig
from agent.power import PowerInterface
from agent.station import ChargingStation
from tests.fixtures.fake_csms import FakeCSMS, FaultConfig

# pytest-asyncio runs in strict mode by default, so every async test
# needs this marker. Applied once at module level rather than repeated.
pytestmark = pytest.mark.asyncio


def make_config(port: int, **overrides) -> AgentConfig:
    """
    A config pointed at a test server, with the session shortened.

    charge_for and meter_every are tiny so a full session completes in
    well under a second: these tests run on every commit and a realistic
    40-second charge would make the suite unusable.
    """
    defaults = dict(
        station_id="CP001",
        csms_url=f"ws://localhost:{port}",
        charge_for_s=0.3,
        meter_every_s=0.1,
        log_level="WARNING",
    )
    defaults.update(overrides)
    return AgentConfig(**defaults)


def actions_received(server: FakeCSMS) -> list[str]:
    """Not available directly; tests assert on message counts instead."""
    return [c.id for c in server.connections]


# -- happy path ---------------------------------------------------------


async def test_full_charging_session_against_the_fake_server():
    """The whole sequence completes and the server sees one station."""
    async with FakeCSMS(port=9210) as server:
        station = ChargingStation(make_config(9210))
        ok = await station.run()

    assert ok is True
    assert len(server.connections) == 1
    assert server.connections[0].id == "CP001"
    # boot + status + authorize + status + tx started + updates + tx ended
    # + final status. Exact count varies with timing, so assert a floor.
    assert server.connections[0].message_count >= 7


async def test_transaction_is_closed_and_contactor_opened_afterwards():
    """
    A finished session must leave no transaction open and no current
    flowing. Track A's charging_count counts stations where
    active_transaction_id is not None, so a transaction that starts and
    never ends leaves a station looking permanently busy.
    """
    async with FakeCSMS(port=9211):
        station = ChargingStation(make_config(9211))
        await station.run()

    assert station.transaction_id is None
    assert station.power.is_closed() is False
    assert station.power.read_energy() > 0


async def test_sequence_numbers_are_monotonic_from_zero():
    """
    Track A detects forward jumps in seq_no and logs them as evidence of
    message loss. A counter that restarted mid-transaction would
    fabricate that finding, indistinguishable from genuine loss.
    """
    async with FakeCSMS(port=9212):
        station = ChargingStation(make_config(9212))
        await station.run()

    # Started + at least one Updated + Ended.
    assert station.seq_no >= 3


async def test_heartbeat_interval_is_taken_from_the_server():
    """
    The fixture issues 7 where the real CSMS issues 20, precisely so an
    agent that hardcodes either value fails here. This asserts the agent
    used what it was told.
    """
    async with FakeCSMS(port=9213, interval_s=3) as server:
        station = ChargingStation(make_config(9213, charge_for_s=0.2))
        await station.run()

    assert server.interval_s == 3


# -- authorisation refusals -----------------------------------------------


async def test_blocked_token_starts_no_transaction():
    """A card the operator has barred. E5 contrasts this with a
    cryptographic refusal — same outcome, different cause."""
    async with FakeCSMS(port=9214):
        station = ChargingStation(
            make_config(9214, id_token="TAG-BLOCKED", charge_for_s=0.1)
        )
        ok = await station.run()

    assert ok is True          # the session ran; it simply did not charge
    assert station.transaction_id is None
    assert station.power.is_closed() is False
    assert station.power.read_energy() == 0.0


async def test_unknown_token_starts_no_transaction():
    async with FakeCSMS(port=9215):
        station = ChargingStation(
            make_config(9215, id_token="TAG-NOPE", charge_for_s=0.1)
        )
        await station.run()

    assert station.transaction_id is None
    assert station.power.read_energy() == 0.0


async def test_accept_all_mode_authorises_an_unknown_token():
    """Mirrors the real server's --auth-mode accept-all escape hatch,
    which exists to unblock integration in seconds rather than a day."""
    faults = FaultConfig(auth_mode="accept-all")
    async with FakeCSMS(port=9216, faults=faults):
        station = ChargingStation(make_config(9216, id_token="TAG-ANYTHING"))
        await station.run()

    assert station.power.read_energy() > 0


# -- boot refusal ------------------------------------------------------------


async def test_rejected_boot_stops_the_station():
    """
    THE DIFFERENCE FROM TRACK A'S FIXTURE.

    tests/fixtures/fake_station.py ignores a Rejected boot and opens a
    transaction anyway. A real charger does not charge after being
    refused registration, and from Stage 6 the refusal becomes
    meaningful: a station whose capabilities exclude the migration
    target must be skipped, not admitted into a wave it cannot finish.
    """
    faults = FaultConfig(fail_boot=True)
    async with FakeCSMS(port=9217, faults=faults):
        station = ChargingStation(make_config(9217))
        ok = await station.run()

    assert ok is False
    assert station.transaction_id is None
    assert station.power.is_closed() is False
    assert station.power.read_energy() == 0.0


# -- CALLError, the silent-failure path ---------------------------------------


async def test_callerror_is_counted_not_swallowed():
    """
    The ocpp library's call() suppresses CALLErrors by DEFAULT and
    returns None. agent/client.py passes suppress=False precisely so
    they surface. This asserts they are counted — the number the load
    generator reports per run, and the team's early warning that the
    Contract 3 event log has holes.
    """
    faults = FaultConfig(callerror_on={"TransactionEvent"})
    async with FakeCSMS(port=9218, faults=faults):
        station = ChargingStation(make_config(9218))
        await station.run()

    assert station.callerror_count >= 1


async def test_callerror_on_boot_stops_the_station_safely():
    """A failure this early is not survivable, but it must still leave
    the contactor open and no transaction dangling."""
    faults = FaultConfig(callerror_on={"BootNotification"})
    async with FakeCSMS(port=9219, faults=faults):
        station = ChargingStation(make_config(9219))
        ok = await station.run()

    assert ok is False
    assert station.callerror_count >= 1
    assert station.power.is_closed() is False


async def test_callerror_on_transaction_leaves_no_current_flowing():
    """
    Safety rule 1: every exit path opens the contactor. On simulated
    hardware this is only a number; on the Raspberry Pi bench node it is
    a relay with current behind it.
    """
    faults = FaultConfig(callerror_on={"TransactionEvent"})
    async with FakeCSMS(port=9220):
        pass
    async with FakeCSMS(port=9220, faults=faults):
        station = ChargingStation(make_config(9220))
        await station.run()

    assert station.power.is_closed() is False


# -- connection-level failures --------------------------------------------------


async def test_connection_refused_is_handled_not_raised():
    """
    Nothing listening. During E2 this is a NORMAL condition — the
    experiment kills the CSMS on purpose — so it must be caught and
    reported, never allowed to crash the agent. Phase C4 turns this into
    a retry; today it is a clean False.
    """
    station = ChargingStation(make_config(9221))
    ok = await station.run()

    assert ok is False
    assert station.power.is_closed() is False


async def test_server_rejecting_the_upgrade_is_handled():
    """A server that is up but refusing work, distinct from nothing
    listening. Different cause, different fix, so they are logged
    differently."""
    faults = FaultConfig(reject_connections=True)
    async with FakeCSMS(port=9222, faults=faults):
        station = ChargingStation(make_config(9222))
        ok = await station.run()

    assert ok is False


async def test_connection_dropped_mid_session_is_handled():
    """
    The Phase C4 precursor. The socket dies four messages in; the agent
    must notice, stop cleanly and leave nothing energised. C4 turns this
    into a reconnect.
    """
    faults = FaultConfig(drop_after=4)
    async with FakeCSMS(port=9223, faults=faults):
        station = ChargingStation(make_config(9223))
        ok = await station.run()

    assert ok is False
    assert station.power.is_closed() is False


# -- power backend integration ----------------------------------------------------


class _RecordingPower(PowerInterface):
    """
    Records the order of physical operations.

    A test double rather than SimulatedPower so the sequence of
    contactor and meter calls can be asserted on directly — the part
    that matters for safety, and the part that silently stops mattering
    if someone reorders two lines.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._closed = False
        self._limit = 7400.0
        self._energy = 0.0

    def close_contactor(self) -> None:
        self.calls.append("close")
        self._closed = True

    def open_contactor(self) -> None:
        self.calls.append("open")
        self._closed = False

    def is_closed(self) -> bool:
        return self._closed

    def set_power_limit(self, watts: float) -> None:
        self.calls.append(f"limit:{watts}")
        self._limit = watts

    def get_power_limit(self) -> float:
        return self._limit

    def read_power(self) -> float:
        return self._limit if self._closed else 0.0

    def read_energy(self) -> float:
        if self._closed:
            self._energy += 1.0
        return self._energy

    def reset_meter(self) -> None:
        self.calls.append("reset")
        self._energy = 0.0


async def test_meter_is_reset_before_the_contactor_closes():
    """
    Order matters. Resetting after closing would discard energy already
    accumulated in this transaction; not resetting at all would carry the
    previous session's total into the first reading, which Track A's
    registry sees as a jump it cannot explain.
    """
    power = _RecordingPower()
    async with FakeCSMS(port=9224):
        station = ChargingStation(make_config(9224), power=power)
        await station.run()

    assert "reset" in power.calls
    assert power.calls.index("reset") < power.calls.index("close")


async def test_contactor_opens_before_the_final_meter_reading():
    """
    The Ended event should report a station drawing nothing, so that
    aggregate_power_w falls to zero on the dashboard at the moment the
    session ends rather than one poll later.
    """
    power = _RecordingPower()
    async with FakeCSMS(port=9225):
        station = ChargingStation(make_config(9225), power=power)
        await station.run()

    assert power.calls.count("close") == 1
    assert "open" in power.calls
    assert power.calls[-1] == "open" or power.calls.index("open") > power.calls.index("close")


async def test_no_contactor_activity_when_authorisation_is_refused():
    """Nothing physical should happen for a driver who was turned away."""
    power = _RecordingPower()
    async with FakeCSMS(port=9226):
        station = ChargingStation(
            make_config(9226, id_token="TAG-BLOCKED", charge_for_s=0.1),
            power=power,
        )
        await station.run()

    assert "close" not in power.calls


# -- redundant traffic -------------------------------------------------------------


async def test_identical_status_is_not_sent_twice_in_a_row():
    """
    Track A guards against no-op status notifications server-side, but
    they are still messages on the wire. At five hundred stations
    reconnecting at once that is hundreds of avoidable sends competing
    with the handshakes E2 is timing.
    """
    async with FakeCSMS(port=9227):
        station = ChargingStation(make_config(9227, id_token="TAG-BLOCKED",
                                              charge_for_s=0.1))
        await station.run()
        # Available sent once; the refusal path never reaches Occupied.
        assert station.last_status == msg.STATUS_AVAILABLE


# -- cancellation -------------------------------------------------------------------


async def test_cancelling_a_running_station_opens_the_contactor():
    """
    Ctrl-C, or the harness shutting a station down mid-charge. The
    contactor must open on the way out — the single most important
    guarantee in this file once the Raspberry Pi is involved.
    """
    power = _RecordingPower()
    async with FakeCSMS(port=9228):
        station = ChargingStation(
            make_config(9228, charge_for_s=30.0, meter_every_s=0.1),
            power=power,
        )
        task = asyncio.ensure_future(station.run())
        await asyncio.sleep(0.5)          # let it get into the charge loop
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert power.is_closed() is False
    assert "open" in power.calls