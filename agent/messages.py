"""
OCPP payload builders — the exact shapes the CSMS expects on the wire.

Track C (agent). Phase C2.

--------------------------------------------------------------------
WHERE THIS FITS

    agent/messages.py   <- you are here. Pure functions. Build dicts.
          |
    agent/client.py        wraps them in ocpp call objects and sends
          |
    agent/station.py       decides WHEN to send them
          |
      the CSMS             csms/metering.py parses what arrives

Nothing here touches the network, asyncio or the ocpp library. That is
deliberate, and mirrors how Track A built csms/metering.py on their
side: "pure functions, no OCPP library import, no I/O -- so it is unit
testable without a running server." The same reasoning applies in the
opposite direction. A wrong measurand string is a one-line mistake that
produces a fleet reporting zero power, and this file exists so that
mistake is caught by a test that runs in milliseconds rather than at
demo rehearsal.

--------------------------------------------------------------------
WHY THE EXACT STRINGS MATTER MORE THAN THEY LOOK

csms/metering.py recognises two measurand labels and nothing else. Send
a different one and the server does NOT crash: it logs
"unrecognised measurand", power_w stays None, aggregate_power_w stays
0.0, and the dashboard shows a fleet charging nothing. Experiment E5's
entire visible payload is a fleet-wide power spike. No power, no spike,
no demonstration.

Track A built that warning path precisely because this mismatch was
considered likely. This module is Track C's half of not causing it.

--------------------------------------------------------------------
ON PLAIN STRINGS INSTEAD OF LIBRARY ENUMS

Every status and enum value here is a plain string, not an import from
ocpp.v201.enums. This follows the convention Track A set in
tests/fixtures/fake_station.py: the enum CLASS names have moved between
releases of the ocpp library, while the wire values are fixed by OCPP
2.0.1 and cannot move. The library's schema validator rejects a wrong
value, so a typo fails loudly at the first message rather than silently.
--------------------------------------------------------------------
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

# -- measurands and units ---------------------------------------------
#
# These four strings are the contract with csms/metering.py. Changing
# any of them silently breaks every power and energy figure in the
# results. They are module constants rather than inline literals so
# that a test can assert on them and so there is exactly one place to
# look when reconciling with Track A.

MEASURAND_POWER = "Power.Active.Import"
"""Instantaneous power draw. csms/metering.py: POWER_MEASURANDS."""

MEASURAND_ENERGY = "Energy.Active.Import.Register"
"""Cumulative energy. csms/metering.py: ENERGY_MEASURANDS, and also its
DEFAULT_MEASURAND -- a sample with no label at all is treated as this."""

UNIT_WATT = "W"
UNIT_WATT_HOUR = "Wh"
"""Base units. Contract 5 (agent/power.py) works in watts and
watt-hours, and csms/metering.py converts to the same base, so sending
base units means no conversion happens anywhere and there is no
multiplier to get wrong."""


# -- connector status (OCPP 2.0.1 ConnectorStatusEnumType) -------------
#
# The physical state of the socket, reported by StatusNotification.
# Track A stores whatever string arrives without converting it, so
# these must be spelled exactly as the specification does.

STATUS_AVAILABLE = "Available"
STATUS_OCCUPIED = "Occupied"
STATUS_RESERVED = "Reserved"
STATUS_UNAVAILABLE = "Unavailable"
STATUS_FAULTED = "Faulted"

CONNECTOR_STATUSES = (
    STATUS_AVAILABLE,
    STATUS_OCCUPIED,
    STATUS_RESERVED,
    STATUS_UNAVAILABLE,
    STATUS_FAULTED,
)


# -- charging state (OCPP 2.0.1 ChargingStateEnumType) ------------------
#
# Carried inside transaction_info. Distinct from connector status:
# "Occupied" says a car is plugged in, "Charging" says current is
# actually flowing. Phase C3's state machine is built on this
# distinction.

CHARGING_STATE_CHARGING = "Charging"
CHARGING_STATE_IDLE = "Idle"
CHARGING_STATE_EV_CONNECTED = "EVConnected"
CHARGING_STATE_SUSPENDED_EV = "SuspendedEV"
CHARGING_STATE_SUSPENDED_EVSE = "SuspendedEVSE"

CHARGING_STATES = (
    CHARGING_STATE_CHARGING,
    CHARGING_STATE_IDLE,
    CHARGING_STATE_EV_CONNECTED,
    CHARGING_STATE_SUSPENDED_EV,
    CHARGING_STATE_SUSPENDED_EVSE,
)


# -- transaction event types --------------------------------------------

TX_STARTED = "Started"
TX_UPDATED = "Updated"
TX_ENDED = "Ended"

TX_EVENT_TYPES = (TX_STARTED, TX_UPDATED, TX_ENDED)


# -- trigger reasons (OCPP 2.0.1 TriggerReasonEnumType) -------------------
#
# Why a TransactionEvent was sent. Track A records this in the event
# payload, so it ends up in the analysis dataset -- which makes it worth
# getting right rather than sending "Authorized" for everything.

TRIGGER_AUTHORIZED = "Authorized"
TRIGGER_METER_PERIODIC = "MeterValuePeriodic"
TRIGGER_STOP_AUTHORIZED = "StopAuthorized"
TRIGGER_EV_DEPARTED = "EVCommunicationLost"
TRIGGER_REMOTE_STOP = "RemoteStop"
TRIGGER_CHARGING_STATE_CHANGED = "ChargingStateChanged"
TRIGGER_CHARGING_RATE_CHANGED = "ChargingRateChanged"


# -- boot reasons ---------------------------------------------------------

BOOT_POWER_UP = "PowerUp"

BOOT_RECONNECT = "PowerUp"
"""
*** CHANGED IN PHASE C4. It used to be "RemoteReset". ***

OCPP 2.0.1's BootReasonEnumType has no "I reconnected after the server
went away" member. The options are ApplicationReset, FirmwareUpdate,
LocalReset, PowerUp, RemoteReset, ScheduledReset, Triggered, Unknown
and Watchdog -- and none of them means "the network came back".

"RemoteReset" was wrong in a way that mattered. On the wire it asserts
that an operator remotely reset this station. Nobody did. Track A logs
the reason verbatim into the Contract 3 event log, so every one of the
hundreds of reconnections in a single E2 run would have recorded a
remote reset that never happened -- and at Stage 9 a log full of them
reads as a CSMS issuing resets under load, which is a finding, and a
false one.

"PowerUp" is what a real charger sends after any restart, including one
caused by losing and regaining its uplink. It is the least wrong true
statement available. The alternative, "Unknown", is defensible but
carries no information at all.

The constant is kept as a separate name rather than collapsed into
BOOT_POWER_UP so the intent stays visible at the call site in
station.py, and so the decision can be revisited in one place if Track
A ever wants reconnects distinguishable in their log by some other
means.
"""


# -- identity types -------------------------------------------------------

ID_TOKEN_TYPE_ISO14443 = "ISO14443"
"""The RFID card type Track A's fixture uses. csms/authorization.py
does not branch on the type -- only the token value -- but the field is
required by the schema."""


DEFAULT_EVSE_ID = 1
DEFAULT_CONNECTOR_ID = 1
"""
One EVSE, one connector per station.

Confirmed with Track A: StationView carries a single ocpp_status, and
Contract 5 gives the agent a single contactor. A station with two
connectors would need both to change, and it buys nothing for any
experiment in the programme. Recorded in docs/limitations.md.
"""


def now_iso() -> str:
    """
    Current time as a timezone-aware ISO-8601 string.

    Timezone-aware is not optional. OCPP requires an offset, and a naive
    timestamp would be rejected by the schema validator -- or worse,
    accepted by a lenient one and then interpreted as local time during
    analysis, silently shifting every event by hours.
    """
    return datetime.now(timezone.utc).isoformat()


def _check_number(name: str, value: float, allow_negative: bool = False) -> float:
    """
    Reject values that would corrupt the dataset rather than crash.

    Three specific dangers:

    NaN and infinity survive Python arithmetic happily -- a division by
    a zero elapsed time produces one -- and json.dumps writes them as
    the bare tokens NaN and Infinity, which are NOT valid JSON. The
    ocpp library would send them, Track A's log would contain them, and
    analysis/parse_events.py would fail at Stage 9 on a file that cannot
    be repaired because the run is over.

    Negative power on an import measurand means the station is exporting
    to the grid, which this project does not model. A negative value
    would drag aggregate_power_w down and quietly understate the fleet
    total that E5 depends on spiking.

    Rejecting here, at build time, means the bad value never reaches the
    wire.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{name} must be a number, got {type(value).__name__}")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError(
            f"{name} must be finite, got {numeric!r}. NaN and infinity are not "
            "valid JSON and would corrupt the event log irreparably."
        )
    if numeric < 0 and not allow_negative:
        raise ValueError(
            f"{name} must be >= 0, got {numeric}. A negative import reading "
            "would understate aggregate fleet power."
        )
    return numeric


def sampled_value(
    value: float,
    measurand: str,
    unit: str,
    multiplier: int | None = None,
) -> dict[str, Any]:
    """
    One reading inside a MeterValue.

    The multiplier is a power of ten that csms/metering.py honours: a
    value of 7.4 with multiplier 3 means 7400. This agent always sends
    base units and no multiplier, so the parameter exists only so a test
    can deliberately exercise the server's multiplier path -- which is
    worth having, because a multiplier silently ignored would make every
    figure in the results wrong by a factor of a thousand in a direction
    nobody would question.
    """
    unit_block: dict[str, Any] = {"unit": unit}
    if multiplier is not None:
        unit_block["multiplier"] = multiplier

    return {
        "value": _check_number("sampled value", value),
        "measurand": measurand,
        "unit_of_measure": unit_block,
    }


def meter_value(
    power_w: float,
    energy_wh: float,
    timestamp: str | None = None,
    *,
    power_measurand: str = MEASURAND_POWER,
    energy_measurand: str = MEASURAND_ENERGY,
    power_unit: str = UNIT_WATT,
    energy_unit: str = UNIT_WATT_HOUR,
) -> dict[str, Any]:
    """
    One OCPP MeterValue carrying instantaneous power and cumulative energy.

    This is the single most integration-critical function in Track C.
    The two measurand strings and two units must match csms/metering.py
    exactly; everything else in the charging flow can be slightly wrong
    and still produce a working demo, but this cannot.

    The four keyword overrides exist for one purpose: testing Track A's
    tripwire. They built a path that logs "unrecognised measurand" by
    name rather than silently reporting zero, and nobody can prove that
    path works without a client capable of sending a wrong label on
    purpose. Production code never passes them.

        # deliberately wrong, to prove the server notices
        meter_value(7400, 12, power_measurand="Power.Wrong.Label")

    Args:
        power_w: instantaneous draw in watts.
        energy_wh: cumulative energy in watt-hours since the meter was
            last reset. Must be non-decreasing across a transaction --
            a real meter never runs backwards, and Track A's registry
            rejects readings older than the last applied one.
        timestamp: ISO-8601; generated if omitted.
    """
    return {
        "timestamp": timestamp or now_iso(),
        "sampled_value": [
            sampled_value(power_w, power_measurand, power_unit),
            sampled_value(energy_wh, energy_measurand, energy_unit),
        ],
    }


def transaction_info(
    transaction_id: str,
    charging_state: str = CHARGING_STATE_CHARGING,
) -> dict[str, Any]:
    """
    Identifies which charging session a TransactionEvent belongs to.

    transaction_id is what ties Started, every Updated and Ended
    together. Track A stores it as active_transaction_id, and the
    dashboard's charging_count is a count of stations where it is not
    None -- so a transaction that starts and never ends leaves a station
    looking permanently busy.
    """
    if charging_state not in CHARGING_STATES:
        raise ValueError(
            f"unknown charging_state {charging_state!r}; "
            f"expected one of {CHARGING_STATES}"
        )
    if not transaction_id:
        raise ValueError("transaction_id must be non-empty")

    return {
        "transaction_id": transaction_id,
        "charging_state": charging_state,
    }


def evse(
    evse_id: int = DEFAULT_EVSE_ID,
    connector_id: int = DEFAULT_CONNECTOR_ID,
) -> dict[str, Any]:
    """
    Which socket on this station. Always 1/1 for us -- see DEFAULT_EVSE_ID.

    Track A records both into the event payload even though the fleet
    view carries only one status, so the values are preserved in the
    dataset should a multi-connector station ever be modelled.
    """
    return {"id": evse_id, "connector_id": connector_id}


def id_token(
    token: str,
    token_type: str = ID_TOKEN_TYPE_ISO14443,
) -> dict[str, Any]:
    """
    A driver's identification, for Authorize and TransactionEvent Started.

    An empty token is allowed through deliberately: csms/authorization.py
    answers Invalid for it, and being able to send one is how the
    "unknown card" path gets tested end to end.
    """
    return {"id_token": token, "type": token_type}


def charging_station(
    model: str = "PQCharge-Agent",
    vendor_name: str = "PQCharge",
    firmware_version: str | None = None,
    serial_number: str | None = None,
) -> dict[str, Any]:
    """
    This station's self-description, sent once in BootNotification.

    Model and vendor appear in Track A's boot log line, so keeping them
    distinct from the fixture's "PQCharge-Fake" makes it obvious at a
    glance whether a log came from the real agent or from
    fake_station.py.

    Optional fields are omitted rather than sent empty: the ocpp library
    strips None values before validating, and an empty string is a
    different thing from an absent field to a strict schema.
    """
    payload: dict[str, Any] = {"model": model, "vendor_name": vendor_name}
    if firmware_version is not None:
        payload["firmware_version"] = firmware_version
    if serial_number is not None:
        payload["serial_number"] = serial_number
    return payload


def authorize_status(response: Any) -> str:
    """
    Pull the decision out of an Authorize response, defensively.

    The response carries id_token_info, a nested object whose "status"
    is the answer. Three things can go wrong and all of them have been
    seen in practice with this library:

      - the whole response is None, because the call was suppressed
        after a CALLError (see the warning in agent/client.py)
      - id_token_info is absent
      - it arrives as an object rather than a dict, depending on how the
        library deserialised it

    Returning the literal string "Unknown" for all three, rather than
    raising, means the agent treats a malformed answer as "not
    authorised" and declines to charge -- which is the safe direction to
    fail in, and is what a real charger does.
    """
    if response is None:
        return "Unknown"

    info = getattr(response, "id_token_info", None)
    if info is None:
        return "Unknown"
    if isinstance(info, dict):
        return str(info.get("status", "Unknown"))
    return str(getattr(info, "status", "Unknown"))