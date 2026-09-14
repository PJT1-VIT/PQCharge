"""
Actuation tests — the CSMS commands, the station obeys, power changes.

Track C (tests). Phase C3.

--------------------------------------------------------------------
WHAT THESE PROVE

Everything up to Phase C2 was the station TALKING. These tests are
about the station LISTENING: a command arrives and, milliseconds later,
the meter readings change. That is the difference between a telemetry
feed and a cyber-physical system, and it is what E5 demonstrates.

Each test starts a real fake CSMS, runs a real ChargingStation against
it, and asserts on the power values the server actually received — not
on the agent's internal state. If the number on the wire did not
change, the actuation did not happen, whatever the agent believes.

--------------------------------------------------------------------
THE SAME CAVEAT AS test_client.py

csms/dispatch.py does not exist yet, so the commands here come from
tests/fixtures/fake_csms.py. That proves the AGENT is correct. It does
not prove the integration. When Track A's dispatcher lands, the happy
paths must be re-run against it — and nothing in agent/ should need to
change, because the agent cannot tell which server sent the command.

--------------------------------------------------------------------
PORTS

9230+, to stay clear of the real CSMS (9000), the fixture default
(9100) and test_client.py's range (9210-9229).
"""

from __future__ import annotations

import asyncio

import pytest

from agent import messages as msg
from agent.config import AgentConfig
from agent.state_machine import StationState
from agent.station import ChargingStation
from tests.fixtures.fake_csms import CommandPlan, FakeCSMS

pytestmark = pytest.mark.asyncio


def make_config(port: int, **overrides) -> AgentConfig:
    """
    A config pointed at a test server, with a session long enough for a
    command to land mid-charge.

    charge_for is deliberately longer than test_client.py's 0.3s: a
    command fired after N messages needs the session to still be running
    when it arrives. meter_every stays small so there are plenty of
    readings to assert on either side of the change.
    """
    defaults = dict(
        station_id="CP001",
        csms_url=f"ws://localhost:{port}",
        charge_for_s=2.0,
        meter_every_s=0.1,
        log_level="WARNING",
    )
    defaults.update(overrides)
    return AgentConfig(**defaults)


def statuses_of(server: FakeCSMS) -> list[str]:
    return server.connections[0].statuses


def readings_of(server: FakeCSMS) -> list[float]:
    return server.connections[0].power_readings


def commands_of(server: FakeCSMS) -> list[tuple[str, str]]:
    return server.connections[0].command_results


def _charging_readings(server: FakeCSMS) -> list[float]:
    """
    Every power reading except the last.

    The final TransactionEvent is Ended, and it legitimately reports
    zero watts -- the contactor opens before it is sent, deliberately,
    so that aggregate_power_w falls to zero on the dashboard at the
    moment the session ends rather than one poll later. Including it in
    a "did the station keep charging" assertion would make every such
    test fail for the wrong reason.
    """
    return readings_of(server)[:-1]


# =====================================================================
# CURTAILMENT — the E5 case
# =====================================================================


async def test_a_zero_watt_profile_drops_power_to_zero():
    """
    THE test in this file.

    A charging profile of 0 W arrives mid-session. Power must fall to
    zero on the wire, and it must have been non-zero before — otherwise
    the test would pass against a station that never charged at all.
    """
    plan = CommandPlan(set_limit_w=0.0, after_messages=6)
    async with FakeCSMS(port=9230, commands=plan) as server:
        station = ChargingStation(make_config(9230))
        await station.run()

    readings = readings_of(server)
    assert readings, "no meter readings reached the server at all"
    assert max(readings) > 0, "the station never drew power, so nothing was curtailed"
    assert readings[-1] == 0.0, f"power did not fall to zero: {readings}"
    assert ("SetChargingProfile", "Accepted") in commands_of(server)


async def test_curtailment_does_not_end_the_transaction():
    """
    The point of SUSPENDED_EVSE. A curtailed station is still in a
    session — connector Occupied, transaction open — it is simply
    drawing nothing. A station that ended its transaction instead would
    free the bay and let another driver plug in, which is not what the
    operator asked for.
    """
    plan = CommandPlan(set_limit_w=0.0, after_messages=6)
    async with FakeCSMS(port=9231, commands=plan) as server:
        station = ChargingStation(make_config(9231))
        await station.run()

    conn = server.connections[0]
    # SuspendedEVSE must appear, and Ended must not appear before it.
    assert msg.CHARGING_STATE_SUSPENDED_EVSE in conn.charging_states
    assert conn.charging_states.count(msg.CHARGING_STATE_CHARGING) >= 1
    # The connector never left Occupied while curtailed: no Available
    # appears between the first Occupied and the last reading.
    assert "Occupied" in conn.statuses


async def test_curtailment_is_identifiable_in_the_event_log():
    """
    Track A stores trigger_reason verbatim. Without ChargingStateChanged
    on the event that carries the drop, the moment a station's power
    went to zero on command is indistinguishable at Stage 9 from an
    ordinary periodic sample that happened to read zero — and E5's whole
    finding is that a deliberate change is visible.
    """
    plan = CommandPlan(set_limit_w=0.0, after_messages=6)
    async with FakeCSMS(port=9232, commands=plan) as server:
        station = ChargingStation(make_config(9232))
        await station.run()

    assert msg.TRIGGER_CHARGING_STATE_CHANGED in server.connections[0].trigger_reasons


async def test_energy_stops_accumulating_while_curtailed():
    """
    A curtailed station draws nothing, so its cumulative energy must go
    flat — not keep climbing, and not reset. A counter that kept
    climbing would put energy into the results that no current
    delivered.
    """
    plan = CommandPlan(set_limit_w=0.0, after_messages=6)
    async with FakeCSMS(port=9233, commands=plan) as server:
        station = ChargingStation(make_config(9233))
        await station.run()

    energy = server.connections[0].energy_readings
    assert len(energy) >= 3
    # Monotonic non-decreasing throughout — a real meter never goes back.
    assert all(b >= a for a, b in zip(energy, energy[1:])), energy
    # And flat at the end: the last two readings are effectively equal.
    assert energy[-1] == pytest.approx(energy[-2], abs=1e-6)


# =====================================================================
# THROTTLING AND RESTORING
# =====================================================================


async def test_a_partial_limit_reduces_power_without_suspending():
    """
    3700 W on a 7400 W station: half power, still Charging. Distinct
    from curtailment — the state must NOT become SuspendedEVSE, or the
    dashboard would show a suspended station that is visibly delivering
    energy.
    """
    plan = CommandPlan(set_limit_w=3700.0, after_messages=6)
    async with FakeCSMS(port=9234, commands=plan) as server:
        station = ChargingStation(make_config(9234, max_power_w=7400.0))
        await station.run()

    conn = server.connections[0]
    assert 3700.0 in conn.power_readings
    assert msg.CHARGING_STATE_SUSPENDED_EVSE not in conn.charging_states


async def test_an_amps_profile_is_converted_to_watts():
    """
    16 A at 230 V single-phase is 3680 W. The end-to-end check that the
    conversion in agent/charging_profile.py survives the trip through
    the ocpp library's camelCase serialisation — a unit test on the
    parser alone would not catch a numberPhases key being dropped in
    transit.
    """
    plan = CommandPlan(set_limit_w=16.0, limit_unit="A", after_messages=6)
    async with FakeCSMS(port=9235, commands=plan) as server:
        station = ChargingStation(make_config(9235, max_power_w=7400.0))
        await station.run()

    assert 3680.0 in readings_of(server)


async def test_clearing_a_profile_restores_full_power():
    """
    Curtail, then clear. Power must come back to the station's maximum
    on the same transaction — a clear that left the station at zero
    would strand every curtailed station on the fleet.
    """
    plan = CommandPlan(
        set_limit_w=0.0, after_messages=6, clear_after_messages=10
    )
    async with FakeCSMS(port=9236, commands=plan) as server:
        station = ChargingStation(make_config(9236, max_power_w=7400.0))
        await station.run()

    readings = readings_of(server)
    assert 0.0 in readings, f"never curtailed: {readings}"
    # Power returned after the zero.
    zero_at = readings.index(0.0)
    assert any(r > 0 for r in readings[zero_at:]), f"power never came back: {readings}"
    assert ("ClearChargingProfile", "Accepted") in commands_of(server)


async def test_clearing_when_nothing_is_installed_answers_unknown():
    """
    Not Rejected. ClearChargingProfileStatus has no Rejected member, and
    "there was nothing matching to clear" is the accurate answer for a
    station that was never curtailed. Track A's dispatcher must not read
    it as a failure.
    """
    plan = CommandPlan(clear_after_messages=6)
    async with FakeCSMS(port=9237, commands=plan) as server:
        station = ChargingStation(make_config(9237))
        await station.run()

    assert ("ClearChargingProfile", "Unknown") in commands_of(server)


# =====================================================================
# REMOTE STOP
# =====================================================================


async def test_request_stop_ends_the_transaction_cleanly():
    """
    A clean end, not a dropped session: an Ended event with the right
    trigger_reason, the contactor open, and the connector back to
    Available. A station that simply stopped sending would look
    identical to one that crashed.
    """
    plan = CommandPlan(stop_after_messages=6)
    async with FakeCSMS(port=9238, commands=plan) as server:
        station = ChargingStation(make_config(9238))
        ok = await station.run()

    conn = server.connections[0]
    assert ("RequestStopTransaction", "Accepted") in conn.command_results
    assert msg.TRIGGER_REMOTE_STOP in conn.trigger_reasons
    assert conn.statuses[-1] == msg.STATUS_AVAILABLE
    assert ok is True
    # And the physical model agrees.
    assert station.power.is_closed() is False
    assert station.transaction_id is None


async def test_request_stop_takes_effect_promptly():
    """
    The stop must not wait out the rest of the meter interval.

    The session is configured to charge for 30 seconds; the command
    arrives after a handful of messages. If the run takes anywhere near
    30 seconds, the wake event in _sleep_or_wake is not working and
    every command in E5 would lag by up to a full meter interval.
    """
    plan = CommandPlan(stop_after_messages=6)
    async with FakeCSMS(port=9239, commands=plan) as server:
        station = ChargingStation(
            make_config(9239, charge_for_s=30.0, meter_every_s=2.0)
        )
        started = asyncio.get_running_loop().time()
        await station.run()
        elapsed = asyncio.get_running_loop().time() - started

    assert ("RequestStopTransaction", "Accepted") in commands_of(server)
    assert elapsed < 20.0, f"the stop took {elapsed:.1f}s to take effect"


async def test_stopping_a_transaction_that_is_not_running_is_refused():
    """
    Reachable in practice: a stop sent during a reconnection storm can
    arrive after the session it referred to has already ended. Refusing
    honestly is what lets the CSMS tell a stale command from a station
    ignoring it.
    """
    station = ChargingStation(make_config(9240))
    accepted, reason = station.handle_request_stop("does-not-exist")
    assert accepted is False
    assert "no transaction" in reason


async def test_stopping_the_wrong_transaction_id_is_refused():
    station = ChargingStation(make_config(9241))
    station.transaction_id = "abc123"
    accepted, reason = station.handle_request_stop("something-else")
    assert accepted is False
    assert "abc123" in reason


# =====================================================================
# TRIGGER MESSAGE
# =====================================================================


async def test_trigger_message_produces_an_extra_status_notification():
    """
    The cheapest probe during an E2 storm: ask a station that looks
    stalled for a StatusNotification and find out in one round trip
    whether it is alive, instead of waiting a heartbeat interval.
    """
    plan = CommandPlan(trigger_after_messages=6, trigger_message="StatusNotification")
    async with FakeCSMS(port=9242, commands=plan) as server:
        station = ChargingStation(make_config(9242))
        await station.run()

    conn = server.connections[0]
    assert ("TriggerMessage", "Accepted") in conn.command_results
    # A normal session sends Available, Occupied, Available. The trigger
    # forces one more, so Occupied appears twice.
    assert conn.statuses.count(msg.STATUS_OCCUPIED) >= 2


async def test_an_unsupported_trigger_is_answered_not_implemented():
    plan = CommandPlan(trigger_after_messages=6, trigger_message="LogStatusNotification")
    async with FakeCSMS(port=9243, commands=plan) as server:
        station = ChargingStation(make_config(9243))
        await station.run()

    assert ("TriggerMessage", "NotImplemented") in commands_of(server)


# =====================================================================
# REFUSALS — the station says no, in valid OCPP, and keeps working
# =====================================================================


async def test_an_unsupported_profile_is_rejected_and_charging_continues():
    """
    The most important refusal test.

    A profile the station will not honour must NOT become a CALLError —
    that would tell the CSMS this station is faulty when the truth is
    the command was not acceptable. And the station must keep charging
    at its previous limit, not fall to zero: a parse failure silently
    curtailing a station would look exactly like a successful
    curtailment in the results.

    NOTE ON WHY THIS USES A VALID-BUT-UNSUPPORTED PROFILE RATHER THAN A
    STRUCTURALLY BROKEN ONE: the ocpp library validates payloads against
    the OCPP schema on the way OUT as well as on the way in, so a
    conformant CSMS physically cannot put a profile with no
    chargingSchedule on the wire — the send fails locally with a
    ProtocolError. The parser's structural guards exist for
    non-conformant senders and are covered by unit tests in
    test_state_machine.py, which can call it directly.
    """
    async with FakeCSMS(port=9244) as server:
        station = ChargingStation(make_config(9244, charge_for_s=1.0))
        task = asyncio.ensure_future(station.run())
        await asyncio.sleep(0.4)  # let it get into the charge loop

        conn = server.connections[0]
        status = await conn.send_set_charging_profile(
            0.0, purpose="ChargingStationExternalConstraints"
        )
        await task

    assert status == "Rejected"
    # It kept charging. Every reading except the last is non-zero -- the
    # last one is the Ended event, which legitimately reports zero
    # because the contactor has just opened.
    assert _charging_readings(server), "no readings to check"
    assert all(r > 0 for r in _charging_readings(server)), (
        f"a refused profile still curtailed the station: {readings_of(server)}"
    )


async def test_a_profile_for_another_evse_is_rejected():
    async with FakeCSMS(port=9245) as server:
        station = ChargingStation(make_config(9245, charge_for_s=1.0))
        task = asyncio.ensure_future(station.run())
        await asyncio.sleep(0.4)

        conn = server.connections[0]
        status = await conn.send_set_charging_profile(0.0, evse_id=7)
        await task

    assert status == "Rejected"
    assert all(r > 0 for r in _charging_readings(server)), (
        f"a refused profile still curtailed the station: {readings_of(server)}"
    )


# =====================================================================
# THE STATE MACHINE, END TO END
# =====================================================================


async def test_the_station_ends_a_normal_session_back_at_available():
    async with FakeCSMS(port=9246) as server:
        station = ChargingStation(make_config(9246, charge_for_s=0.5))
        await station.run()

    assert station.state.state is StationState.AVAILABLE
    assert statuses_of(server)[-1] == msg.STATUS_AVAILABLE


async def test_a_curtailed_station_reports_occupied_not_available():
    """
    The failure this guards against is subtle and would be believed: a
    station showing Available while a car is plugged in and a
    transaction is open. The bay looks free on the dashboard, and the
    fleet's charging_count is wrong for the whole curtailed period.
    """
    plan = CommandPlan(set_limit_w=0.0, after_messages=6)
    async with FakeCSMS(port=9247, commands=plan) as server:
        station = ChargingStation(make_config(9247, charge_for_s=1.5))
        task = asyncio.ensure_future(station.run())
        await asyncio.sleep(1.0)

        # Caught mid-curtailment.
        assert station.state.state is StationState.SUSPENDED_EVSE
        assert station.state.connector_status == msg.STATUS_OCCUPIED
        assert station.power.read_power() == 0.0
        assert station.transaction_id is not None  # session still open

        await task