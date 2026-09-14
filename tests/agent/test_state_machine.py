"""
Tests for agent/state_machine.py and agent/charging_profile.py.

Track C (tests). Phase C3.

No network, no event loop, no ocpp library — these run in milliseconds.
Both modules under test are pure logic, which is the whole reason they
were split out of station.py: the mapping table and the amps conversion
are exactly the kind of code that is wrong in a way no exception
reveals.
"""

from __future__ import annotations

import pytest

from agent import messages as msg
from agent.charging_profile import (
    NOMINAL_VOLTAGE_V,
    ProfileRejected,
    cleared_limit_w,
    parse_charging_profile,
)
from agent.state_machine import (
    IllegalTransition,
    StationState,
    StationStateMachine,
)


# =====================================================================
# THE MAPPING TABLE — one state, two wire fields
# =====================================================================


@pytest.mark.parametrize(
    "state, connector_status, charging_state, draws_power",
    [
        (StationState.AVAILABLE, "Available", None, False),
        (StationState.OCCUPIED, "Occupied", None, False),
        (StationState.CHARGING, "Occupied", "Charging", True),
        (StationState.SUSPENDED_EV, "Occupied", "SuspendedEV", False),
        (StationState.SUSPENDED_EVSE, "Occupied", "SuspendedEVSE", False),
        (StationState.FAULTED, "Faulted", None, False),
    ],
)
def test_every_state_maps_to_the_exact_wire_strings(
    state, connector_status, charging_state, draws_power
):
    """
    Hardcoded on purpose.

    These strings are the contract with csms/handlers.py and
    csms/registry.py. Track A stores whatever arrives without converting
    it, so a typo is a silent data error rather than a crash — the
    dashboard shows a state nobody recognises and the analysis at Stage
    9 has a column full of it.
    """
    sm = StationStateMachine(initial=state)
    assert sm.connector_status == connector_status
    assert sm.charging_state == charging_state
    assert sm.draws_power is draws_power


def test_mapping_uses_the_constants_from_messages():
    """
    The literals above must be the same strings agent/messages.py
    exports. If someone renames a constant there, this catches it.
    """
    sm = StationStateMachine(initial=StationState.SUSPENDED_EVSE)
    assert sm.connector_status == msg.STATUS_OCCUPIED
    assert sm.charging_state == msg.CHARGING_STATE_SUSPENDED_EVSE


def test_suspended_ev_and_suspended_evse_are_different_states():
    """
    They look identical on a dashboard and mean opposite things: one is
    the car's decision, one is the operator's. Reporting the wrong one
    makes a deliberate curtailment indistinguishable from a full
    battery in the E5 results.
    """
    ev = StationStateMachine(initial=StationState.SUSPENDED_EV)
    evse = StationStateMachine(initial=StationState.SUSPENDED_EVSE)
    assert ev.charging_state != evse.charging_state


def test_only_charging_draws_power():
    for state in StationState:
        sm = StationStateMachine(initial=state)
        assert sm.draws_power is (state is StationState.CHARGING)


def test_in_transaction_matches_whether_charging_state_is_sent():
    """
    A transaction is open exactly when there is a charging_state to
    report. If these two ever disagree, the agent would either send a
    charging_state with no transaction or omit one that Track A needs to
    tie its events together.
    """
    for state in StationState:
        sm = StationStateMachine(initial=state)
        assert sm.in_transaction is (sm.charging_state is not None)


# =====================================================================
# TRANSITIONS
# =====================================================================


def test_a_normal_session_walks_through_the_expected_states():
    sm = StationStateMachine()
    assert sm.state is StationState.AVAILABLE

    assert sm.transition_to(StationState.OCCUPIED, "plugged in") is True
    assert sm.transition_to(StationState.CHARGING, "started") is True
    assert sm.transition_to(StationState.OCCUPIED, "ended") is True
    assert sm.transition_to(StationState.AVAILABLE, "unplugged") is True

    assert sm.transition_count == 4


def test_a_self_transition_is_a_no_op_and_reports_no_change():
    """
    The return value is what station.py uses to decide whether a
    StatusNotification is worth sending. Re-sending an identical status
    is harmless to Track A but is still a message on the wire, and at
    five hundred stations during an E2 storm those add up on a server
    the experiment is deliberately overloading.
    """
    sm = StationStateMachine(initial=StationState.CHARGING)
    assert sm.transition_to(StationState.CHARGING, "again") is False
    assert sm.transition_count == 0


def test_the_curtailment_round_trip():
    """The E5 sequence: charging -> curtailed -> charging again."""
    sm = StationStateMachine(initial=StationState.CHARGING)

    assert sm.transition_to(StationState.SUSPENDED_EVSE, "0 W profile") is True
    assert sm.draws_power is False
    assert sm.in_transaction is True  # the session did NOT end

    assert sm.transition_to(StationState.CHARGING, "profile lifted") is True
    assert sm.draws_power is True


@pytest.mark.parametrize(
    "start, target",
    [
        # Nothing plugged in cannot start charging.
        (StationState.AVAILABLE, StationState.CHARGING),
        (StationState.AVAILABLE, StationState.SUSPENDED_EV),
        (StationState.AVAILABLE, StationState.SUSPENDED_EVSE),
        # A faulted station cannot resume charging without first
        # returning to empty.
        (StationState.FAULTED, StationState.CHARGING),
        (StationState.FAULTED, StationState.OCCUPIED),
    ],
)
def test_illegal_transitions_raise(start, target):
    """
    Loud rather than lenient. There is no sequence of server commands
    that can request one of these — station.py maps every command onto a
    legal target — so reaching here means a bug, and a bug that silently
    left the station reporting Available while current flowed would be
    invisible until the results were wrong.
    """
    sm = StationStateMachine(initial=start)
    with pytest.raises(IllegalTransition):
        sm.transition_to(target, "should not be possible")
    assert sm.state is start  # and nothing changed


def test_can_agrees_with_transition_to():
    """
    The OCPP handlers call can() to decide Accepted/Rejected BEFORE
    changing anything. If it disagreed with transition_to(), the agent
    would accept a command on the wire and then fail to carry it out —
    which is the one thing agent/client.py's rule 3 forbids.
    """
    for start in StationState:
        for target in StationState:
            sm = StationStateMachine(initial=start)
            if sm.can(target):
                sm.transition_to(target, "allowed")
            else:
                with pytest.raises(IllegalTransition):
                    sm.transition_to(target, "not allowed")


def test_fault_is_reachable_from_every_state():
    """A safety stop must never be blocked by a transition table."""
    for start in StationState:
        sm = StationStateMachine(initial=start)
        sm.fault("emergency")
        assert sm.state is StationState.FAULTED
        assert sm.draws_power is False


def test_reset_is_reachable_from_every_state():
    """"Nothing is plugged in" is always a physically reachable truth."""
    for start in StationState:
        sm = StationStateMachine(initial=start)
        sm.reset()
        assert sm.state is StationState.AVAILABLE


def test_on_change_fires_only_for_real_changes():
    seen = []
    sm = StationStateMachine(
        on_change=lambda prev, now, reason: seen.append((prev, now, reason))
    )

    sm.transition_to(StationState.OCCUPIED, "plugged in")
    sm.transition_to(StationState.OCCUPIED, "plugged in again")  # no-op

    assert seen == [(StationState.AVAILABLE, StationState.OCCUPIED, "plugged in")]


# =====================================================================
# CHARGING PROFILE PARSING
# =====================================================================


def _profile(limit, unit="W", **extra):
    """A minimal valid profile, wire spelling (camelCase)."""
    period = {"startPeriod": 0, "limit": limit}
    period.update(extra.pop("period", {}))
    return {
        "id": 100,
        "stackLevel": 0,
        "chargingProfilePurpose": "TxDefaultProfile",
        "chargingProfileKind": "Absolute",
        "chargingSchedule": [
            {
                "id": 1,
                "chargingRateUnit": unit,
                "chargingSchedulePeriod": [period],
            }
        ],
        **extra,
    }


def test_a_watt_limit_comes_through_unchanged():
    limit = parse_charging_profile(_profile(3700.0), max_power_w=7400.0)
    assert limit.watts == 3700.0
    assert limit.unit == "W"
    assert limit.clamped is False
    assert limit.is_curtailment is False


def test_zero_is_recognised_as_curtailment():
    """
    The E5 case. Zero must be a valid limit, not an error — the whole
    demonstration is a station told to draw nothing while its
    transaction stays open.
    """
    limit = parse_charging_profile(_profile(0), max_power_w=7400.0)
    assert limit.watts == 0.0
    assert limit.is_curtailment is True


def test_snake_case_payloads_parse_identically():
    """
    The ocpp library snake_cases payload keys recursively before calling
    a handler, so this is the spelling the agent actually sees in
    production. The camelCase spelling is what tests and captured
    traffic carry. Both must work or the tests prove nothing.
    """
    snake = {
        "id": 100,
        "stack_level": 0,
        "charging_profile_purpose": "TxDefaultProfile",
        "charging_profile_kind": "Absolute",
        "charging_schedule": [
            {
                "id": 1,
                "charging_rate_unit": "W",
                "charging_schedule_period": [{"start_period": 0, "limit": 3700.0}],
            }
        ],
    }
    assert parse_charging_profile(snake, max_power_w=7400.0).watts == 3700.0


def test_amps_are_converted_using_the_documented_voltage():
    """
    The single most plausible place for a silent factor-of-230 error in
    the whole actuation path. 16 A single-phase at 230 V is 3680 W — a
    standard domestic charging circuit, so the number is recognisable
    and a wrong conversion is obvious on sight.
    """
    limit = parse_charging_profile(_profile(16, unit="A"), max_power_w=7400.0)
    assert limit.watts == pytest.approx(16 * NOMINAL_VOLTAGE_V)
    assert limit.watts == pytest.approx(3680.0)
    assert limit.raw_limit == 16.0  # the original is kept for the log


def test_three_phase_amps_multiply_by_the_phase_count():
    limit = parse_charging_profile(
        _profile(16, unit="A", period={"numberPhases": 3}),
        max_power_w=20000.0,
    )
    assert limit.watts == pytest.approx(16 * NOMINAL_VOLTAGE_V * 3)
    assert limit.number_phases == 3


def test_a_limit_above_the_station_maximum_is_clamped_not_refused():
    """
    A real charger physically cannot exceed its rating. Refusing would
    leave the station at its OLD limit, which is the opposite of what
    the operator asked for — so the request is honoured as far as the
    hardware allows and the clamping is reported.
    """
    limit = parse_charging_profile(_profile(22000.0), max_power_w=7400.0)
    assert limit.watts == 7400.0
    assert limit.clamped is True
    assert limit.raw_limit == 22000.0


def test_evse_zero_means_the_whole_station():
    assert parse_charging_profile(
        _profile(3700.0), max_power_w=7400.0, evse_id=0
    ).watts == 3700.0


def test_a_profile_for_another_evse_is_rejected():
    with pytest.raises(ProfileRejected, match="not this station"):
        parse_charging_profile(_profile(3700.0), max_power_w=7400.0, evse_id=2)


@pytest.mark.parametrize(
    "payload, match",
    [
        (None, "missing"),
        ({}, "chargingSchedule"),
        ({"chargingSchedule": []}, "empty"),
        ({"chargingSchedule": [{"chargingRateUnit": "W"}]}, "chargingSchedulePeriod"),
        (
            {"chargingSchedule": [
                {"chargingRateUnit": "W", "chargingSchedulePeriod": []}
            ]},
            "empty",
        ),
    ],
    ids=["no-profile", "no-schedule", "empty-schedule", "no-period", "empty-period"],
)
def test_malformed_profiles_are_rejected_rather_than_crashing(payload, match):
    """
    Every one of these is a KeyError or an IndexError waiting to happen,
    four levels deep in a payload that is perfectly valid OCPP and just
    shaped differently from the one example somebody tested against.

    ProfileRejected rather than an exception escaping is the whole point:
    an exception inside an @on handler becomes a CALLError, which tells
    the CSMS this station is FAULTY. A malformed command is not a
    station fault.
    """
    with pytest.raises(ProfileRejected, match=match):
        parse_charging_profile(payload, max_power_w=7400.0)


def test_a_negative_limit_is_rejected():
    """
    Negative import means exporting to the grid, which this project does
    not model — and SimulatedPower.set_power_limit raises on it, which
    from inside a handler would become the CALLError above.
    """
    with pytest.raises(ProfileRejected, match=">= 0"):
        parse_charging_profile(_profile(-100.0), max_power_w=7400.0)


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_non_finite_limits_are_rejected(bad):
    with pytest.raises(ProfileRejected, match="finite"):
        parse_charging_profile(_profile(bad), max_power_w=7400.0)


def test_a_boolean_limit_is_rejected():
    """True is 1 in Python arithmetic, so a stray boolean would become a
    limit of one watt — which looks like curtailment and is not."""
    with pytest.raises(ProfileRejected, match="must be a number"):
        parse_charging_profile(_profile(True), max_power_w=7400.0)


def test_an_unknown_rate_unit_is_rejected():
    with pytest.raises(ProfileRejected, match="chargingRateUnit"):
        parse_charging_profile(_profile(10, unit="kW"), max_power_w=7400.0)


def test_external_constraints_profiles_are_refused():
    """
    ChargingStationExternalConstraints describes limits imposed outside
    the CSMS. Accepting one would mean claiming to enforce something
    this station knows nothing about.
    """
    profile = _profile(3700.0)
    profile["chargingProfilePurpose"] = "ChargingStationExternalConstraints"
    with pytest.raises(ProfileRejected, match="not supported"):
        parse_charging_profile(profile, max_power_w=7400.0)


def test_clearing_returns_the_station_maximum_not_zero():
    """
    The mistake this guards against would stop every charging session on
    the fleet the moment an operator tidied up their profiles — and in
    an experiment log that reads as a cryptography failure.
    """
    assert cleared_limit_w(7400.0) == 7400.0