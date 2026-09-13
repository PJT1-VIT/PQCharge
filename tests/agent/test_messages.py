"""
Tests for agent/messages.py — the payload shapes.

Track C (tests). Phase C2.

Fast and serverless: no network, no event loop, no ocpp library. These
run in milliseconds and exist to catch the one class of mistake that is
invisible until demo rehearsal -- a measurand string or unit that does
not match csms/metering.py, which makes the whole fleet report zero
power while nothing crashes.

The constants asserted here are duplicated on purpose. If someone
"tidies up" MEASURAND_POWER, this file fails and names the string it
expected, rather than the failure surfacing later as an empty chart.
"""

from __future__ import annotations

import json
import math
from datetime import datetime

import pytest

from agent import messages as msg


# -- the four strings that matter most ---------------------------------


def test_measurand_and_unit_constants_match_the_server():
    """
    Hardcoded on purpose — see the module docstring.

    These four values are the contract with csms/metering.py:
    POWER_MEASURANDS, ENERGY_MEASURANDS and its unit scale table.
    """
    assert msg.MEASURAND_POWER == "Power.Active.Import"
    assert msg.MEASURAND_ENERGY == "Energy.Active.Import.Register"
    assert msg.UNIT_WATT == "W"
    assert msg.UNIT_WATT_HOUR == "Wh"


# -- meter value shape --------------------------------------------------


def test_meter_value_shape_matches_what_the_csms_parses():
    mv = msg.meter_value(7400.0, 12.5)

    assert set(mv) == {"timestamp", "sampled_value"}
    assert len(mv["sampled_value"]) == 2

    power, energy = mv["sampled_value"]

    assert power["value"] == 7400.0
    assert power["measurand"] == "Power.Active.Import"
    assert power["unit_of_measure"] == {"unit": "W"}

    assert energy["value"] == 12.5
    assert energy["measurand"] == "Energy.Active.Import.Register"
    assert energy["unit_of_measure"] == {"unit": "Wh"}


def test_meter_value_power_comes_first():
    """
    csms/metering.py reads the measurand label rather than the position,
    so order is not strictly required — but a stable order keeps log
    lines and captured traffic readable, and a reordering would be a
    silent change to every recorded run.
    """
    mv = msg.meter_value(1.0, 2.0)
    assert mv["sampled_value"][0]["measurand"] == msg.MEASURAND_POWER


def test_meter_value_accepts_an_explicit_timestamp():
    mv = msg.meter_value(1.0, 2.0, timestamp="2026-09-13T10:00:00+00:00")
    assert mv["timestamp"] == "2026-09-13T10:00:00+00:00"


def test_meter_value_generates_a_timezone_aware_timestamp():
    """
    A naive timestamp would either be rejected by the schema or, worse,
    accepted and read as local time at analysis, shifting every event.
    """
    parsed = datetime.fromisoformat(msg.meter_value(1.0, 2.0)["timestamp"])
    assert parsed.tzinfo is not None


def test_meter_value_is_json_serialisable():
    """It travels over the wire as JSON and lands in Track A's log."""
    json.dumps(msg.meter_value(7400.0, 12.5))


def test_measurand_can_be_overridden_to_test_the_server_tripwire():
    """
    Track A logs 'unrecognised measurand' by name rather than silently
    reporting zero. Proving that path works needs a client that can send
    a wrong label deliberately.
    """
    mv = msg.meter_value(7400.0, 1.0, power_measurand="Power.Wrong.Label")
    assert mv["sampled_value"][0]["measurand"] == "Power.Wrong.Label"


def test_multiplier_is_omitted_unless_asked_for():
    """
    We always send base units. The multiplier path exists only so a test
    can prove the server honours it — an ignored multiplier would make
    every figure wrong by a factor of a thousand.
    """
    assert "multiplier" not in msg.sampled_value(1.0, "M", "W")["unit_of_measure"]

    with_mult = msg.sampled_value(7.4, "M", "kW", multiplier=3)
    assert with_mult["unit_of_measure"]["multiplier"] == 3


# -- numeric guards ------------------------------------------------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_values_are_rejected(bad):
    """
    NaN and Infinity are not valid JSON. They survive Python arithmetic
    happily — a division by a zero elapsed time produces one — and would
    reach Track A's log, where they break analysis at Stage 9 on a file
    that cannot be repaired because the run is over.
    """
    with pytest.raises(ValueError, match="finite"):
        msg.meter_value(bad, 1.0)


def test_negative_power_is_rejected():
    """
    Negative import means exporting to the grid, which this project does
    not model. It would drag aggregate_power_w down and understate the
    fleet total that E5 depends on spiking.
    """
    with pytest.raises(ValueError, match=">= 0"):
        msg.meter_value(-1.0, 1.0)


def test_negative_energy_is_rejected():
    with pytest.raises(ValueError, match=">= 0"):
        msg.meter_value(1.0, -1.0)


def test_zero_is_allowed():
    """
    The Ended event legitimately reports zero power — the contactor has
    just opened — so zero must not be treated as invalid.
    """
    mv = msg.meter_value(0.0, 0.0)
    assert mv["sampled_value"][0]["value"] == 0.0


def test_booleans_are_rejected_as_values():
    """
    True is 1 in Python arithmetic, so a stray boolean would sail
    through a naive numeric check and be sent as a power reading of 1 W.
    """
    with pytest.raises(TypeError):
        msg.meter_value(True, 1.0)


def test_integers_are_accepted_and_normalised_to_float():
    mv = msg.meter_value(7400, 12)
    assert mv["sampled_value"][0]["value"] == 7400.0
    assert isinstance(mv["sampled_value"][0]["value"], float)


# -- transaction_info ------------------------------------------------------


def test_transaction_info_shape():
    info = msg.transaction_info("abc123")
    assert info == {"transaction_id": "abc123", "charging_state": "Charging"}


def test_transaction_info_accepts_every_known_charging_state():
    for state in msg.CHARGING_STATES:
        assert msg.transaction_info("t", state)["charging_state"] == state


def test_unknown_charging_state_is_rejected():
    with pytest.raises(ValueError, match="unknown charging_state"):
        msg.transaction_info("t", "Charginggg")


def test_empty_transaction_id_is_rejected():
    """
    An empty id would leave Track A unable to tie Started, Updated and
    Ended together, and the station would look permanently busy.
    """
    with pytest.raises(ValueError, match="non-empty"):
        msg.transaction_info("")


# -- evse and id_token ------------------------------------------------------


def test_evse_defaults_to_one_and_one():
    assert msg.evse() == {"id": 1, "connector_id": 1}


def test_id_token_shape():
    assert msg.id_token("TAG-0001") == {
        "id_token": "TAG-0001",
        "type": "ISO14443",
    }


def test_empty_id_token_is_allowed():
    """
    csms/authorization.py answers Invalid for it, and being able to send
    one is how the unknown-card path gets tested end to end.
    """
    assert msg.id_token("")["id_token"] == ""


# -- charging_station -------------------------------------------------------


def test_charging_station_omits_optional_fields():
    """
    An absent field and an empty string are different things to a strict
    schema, so optional values are left out rather than sent blank.
    """
    cs = msg.charging_station()
    assert set(cs) == {"model", "vendor_name"}


def test_charging_station_includes_optionals_when_given():
    cs = msg.charging_station(firmware_version="1.2.3", serial_number="SN1")
    assert cs["firmware_version"] == "1.2.3"
    assert cs["serial_number"] == "SN1"


def test_agent_identifies_itself_distinctly_from_the_fixture():
    """
    Track A's fixture reports PQCharge-Fake. The real agent must not, or
    a boot line in their log is ambiguous about what produced it.
    """
    assert msg.charging_station()["model"] != "PQCharge-Fake"


# -- authorize_status, the defensive parser -----------------------------------


class _Resp:
    def __init__(self, info):
        self.id_token_info = info


class _Info:
    def __init__(self, status):
        self.status = status


def test_authorize_status_reads_a_dict():
    assert msg.authorize_status(_Resp({"status": "Accepted"})) == "Accepted"


def test_authorize_status_reads_an_object():
    """The library has returned both shapes across releases."""
    assert msg.authorize_status(_Resp(_Info("Blocked"))) == "Blocked"


@pytest.mark.parametrize(
    "response",
    [None, _Resp(None), _Resp({})],
    ids=["response-is-None", "info-is-None", "info-is-empty"],
)
def test_authorize_status_fails_closed(response):
    """
    A None response is what a suppressed CALLError leaves behind. All
    three malformed cases return "Unknown", which is not "Accepted", so
    the station declines to charge — failing in the safe direction, as a
    real charger does.
    """
    assert msg.authorize_status(response) == "Unknown"
    assert msg.authorize_status(response) != "Accepted"


# -- constants sanity ----------------------------------------------------------


def test_status_and_event_constants_are_distinct():
    """Guards against a copy-paste duplicate silently collapsing two
    states into one."""
    assert len(set(msg.CONNECTOR_STATUSES)) == len(msg.CONNECTOR_STATUSES)
    assert len(set(msg.CHARGING_STATES)) == len(msg.CHARGING_STATES)
    assert len(set(msg.TX_EVENT_TYPES)) == len(msg.TX_EVENT_TYPES)


def test_now_iso_is_parseable_and_aware():
    parsed = datetime.fromisoformat(msg.now_iso())
    assert parsed.tzinfo is not None