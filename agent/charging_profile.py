"""
SetChargingProfile parsing — one command in, one number out.

Track C (agent). Phase C3.

--------------------------------------------------------------------
WHERE THIS FITS

    the CSMS sends SetChargingProfile
          |
    agent/client.py             the @on handler receives it
          |
    agent/charging_profile.py <- you are here. Pure functions. Digs the
          |                      limit out of a four-level-deep payload
          |                      and converts it to watts.
    agent/station.py             applies it via Contract 5
          |
    agent/power.py               set_power_limit(watts)

Nothing here touches the network, asyncio, the ocpp library or the
power backend. Same reasoning as agent/messages.py: this is the kind of
code that is wrong in a way no exception reveals -- an amps-to-watts
conversion off by a factor of 230 still produces a plausible number --
so it is written to be unit testable in milliseconds.

--------------------------------------------------------------------
WHAT AN OCPP 2.0.1 CHARGING PROFILE ACTUALLY LOOKS LIKE

The limit is buried. In wire JSON (camelCase):

    {
      "evseId": 1,
      "chargingProfile": {
        "id": 100,
        "stackLevel": 0,
        "chargingProfilePurpose": "TxDefaultProfile",
        "chargingProfileKind": "Absolute",
        "chargingSchedule": [                      <- a LIST
          {
            "id": 1,
            "chargingRateUnit": "W",               <- W or A
            "chargingSchedulePeriod": [            <- also a LIST
              {"startPeriod": 0, "limit": 3700.0}  <- the number
            ]
          }
        ]
      }
    }

Four levels down, through two lists. Every one of those levels is a
place to get an IndexError or a KeyError from a payload that is
perfectly valid OCPP but shaped differently from the one example
somebody tested against.

--------------------------------------------------------------------
*** THIS FILE NEVER RAISES AT ITS CALLER ***

Everything that can go wrong comes back as ProfileRejected, carrying a
human-readable reason. It does NOT propagate an exception out to the
OCPP handler, and that is the whole point:

    an exception in an @on handler  -> the ocpp library turns it into a
                                       CALLError
    a CALLError for SetChargingProfile -> the CSMS logs a protocol
                                       fault and, in Track A's
                                       dispatcher, may mark the station
                                       unreachable

A malformed profile is NOT a protocol fault. The correct OCPP answer is
a normal response with status "Rejected" and a statusInfo explaining
why. The station stays connected, stays charging at its previous limit,
and the operator can see exactly what was wrong with their command.

--------------------------------------------------------------------
ON ACCEPTING BOTH camelCase AND snake_case

The ocpp library snake_cases payload keys recursively before calling a
handler, so in production this module sees "charging_schedule". But
tests, captured traffic and anything hand-written carry the wire
spelling. Reading both costs one helper function and means the same
parser can be pointed at a raw OCPP JSON sample -- which is how the
tests are written.
--------------------------------------------------------------------
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from agent.logging_setup import get_logger

LOG = get_logger(__name__)


# -- the amps-to-watts assumption, in exactly one place --------------------

NOMINAL_VOLTAGE_V = 230.0
"""
Line voltage assumed when a profile is expressed in amps.

*** THIS IS AN ASSUMPTION, NOT A MEASUREMENT. ***

A real station knows its supply voltage; a simulated one cannot. 230 V
single-phase is the European AC charging norm and matches the 7.4 kW
figure in agent/simulated_power.py (7400 W / 230 V ~= 32 A, a standard
single-phase charging circuit).

This constant exists so the assumption is written down once, is
greppable, and appears in the log line whenever it is actually used --
rather than being an unexplained 230 multiplied in somewhere. If Track
A's dispatcher sends profiles in W, as recommended, it is never used at
all.
"""

DEFAULT_NUMBER_PHASES = 1
"""Single-phase unless the profile says otherwise."""

UNIT_W = "W"
UNIT_A = "A"
CHARGING_RATE_UNITS = (UNIT_W, UNIT_A)

# OCPP 2.0.1 ChargingProfilePurposeEnumType. Listed so that an unknown
# value is refused by name rather than silently treated as a normal
# limit -- a profile purpose we do not understand may mean something
# quite different from "cap the power".
PURPOSE_TX = "TxProfile"
PURPOSE_TX_DEFAULT = "TxDefaultProfile"
PURPOSE_STATION_MAX = "ChargingStationMaxProfile"
PURPOSE_EXTERNAL = "ChargingStationExternalConstraints"

SUPPORTED_PURPOSES = (PURPOSE_TX, PURPOSE_TX_DEFAULT, PURPOSE_STATION_MAX)
"""
What this agent honours.

ChargingStationExternalConstraints is deliberately absent: it describes
constraints imposed outside the CSMS (a building energy management
system), and accepting one would mean claiming to enforce something
this station has no knowledge of.
"""


class ProfileRejected(Exception):
    """
    This profile cannot be applied, and here is why in plain words.

    The `reason` is sent back to the CSMS inside statusInfo, so it ends
    up in Track A's event log. Write reasons an operator can act on
    ("chargingSchedule was empty"), not internal ones ("index 0").
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ProfileLimit:
    """
    A parsed profile, reduced to what the agent can actually act on.

    Frozen because it is a fact about a message that has already
    arrived -- nothing downstream should be editing it.
    """

    watts: float
    """The limit to hand to Contract 5. Always watts, always >= 0,
    always already clamped to the station's own maximum."""

    raw_limit: float
    """The number as it arrived, before conversion or clamping. Logged
    so a surprising watts value can be traced back to what was sent."""

    unit: str
    """"W" or "A", as it arrived."""

    number_phases: int
    """Phases used in the A -> W conversion. 1 unless stated."""

    clamped: bool
    """
    True when the requested limit exceeded the station's hardware
    maximum and was reduced.

    Worth reporting rather than hiding: a CSMS that thinks it raised a
    station to 22 kW when the station is a 7.4 kW unit will compute a
    fleet capacity that does not exist, and E5's aggregate figures come
    from exactly that kind of arithmetic.
    """

    profile_id: Any = None
    purpose: str | None = None
    stack_level: Any = None

    @property
    def is_curtailment(self) -> bool:
        """
        A limit of zero: draw nothing, but keep the transaction open.

        This is the SUSPENDED_EVSE case and the thing E5 demonstrates.
        Compared against a small epsilon rather than == 0.0 because the
        value may have come through an amps conversion.
        """
        return self.watts <= 1e-9

    def describe(self) -> str:
        """One line, for the log."""
        converted = (
            f"{self.raw_limit:g}{self.unit}"
            f" x {NOMINAL_VOLTAGE_V:g}V x {self.number_phases}ph"
            if self.unit == UNIT_A
            else f"{self.raw_limit:g}{self.unit}"
        )
        return (
            f"{converted} -> {self.watts:.1f}W"
            f"{' (clamped to station maximum)' if self.clamped else ''}"
            f"{' [CURTAILMENT: 0 W]' if self.is_curtailment else ''}"
            f" profile_id={self.profile_id} purpose={self.purpose}"
        )


# -- reading a payload that may be spelled either way -----------------------


def _get(payload: Any, *names: str, default: Any = None) -> Any:
    """
    First present key out of several spellings.

    Handles the camelCase / snake_case question described in the module
    docstring without every call site having to think about it.
    """
    if not isinstance(payload, dict):
        return default
    for name in names:
        if name in payload and payload[name] is not None:
            return payload[name]
    return default


def _first_of_list(value: Any, what: str) -> Any:
    """
    The first entry of a list that OCPP says is a list.

    OCPP 2.0.1 allows multiple schedules and multiple periods; this
    agent honours the first of each and says so. Supporting a full
    time-varying schedule would mean a scheduler inside the agent, which
    is well beyond a simulated station and beyond anything the
    experiments need -- every profile the dispatcher sends has exactly
    one period starting at 0.

    A dict is also accepted, because a hand-written test payload or a
    non-conformant CSMS may send a bare object where a list is
    specified, and refusing it would fail for a reason nobody could
    diagnose from the wire.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, (list, tuple)):
        if not value:
            raise ProfileRejected(f"{what} was empty")
        if len(value) > 1:
            LOG.warning(
                "profile carried %d %s entries; this agent honours the first "
                "and ignores the rest", len(value), what,
            )
        return value[0]
    raise ProfileRejected(f"{what} was missing")


def _as_float(value: Any, what: str) -> float:
    """
    A finite, non-negative float, or a rejection.

    bool is excluded explicitly: True is 1 in Python arithmetic, so a
    stray boolean would sail through a naive numeric check and become a
    power limit of one watt -- which looks like curtailment and is not.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProfileRejected(f"{what} must be a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ProfileRejected(f"{what} must be finite, got {value!r}")
    if number < 0:
        # A negative limit would mean exporting to the grid. This
        # project does not model that, and SimulatedPower.set_power_limit
        # raises on it -- which, from inside an OCPP handler, would
        # become a CALLError. Catching it here turns it into a clean
        # Rejected instead.
        raise ProfileRejected(f"{what} must be >= 0, got {number}")
    return number


# -- the parser ---------------------------------------------------------------


def parse_charging_profile(
    charging_profile: Any,
    *,
    max_power_w: float,
    evse_id: Any = None,
    station_evse_id: int = 1,
) -> ProfileLimit:
    """
    Turn one SetChargingProfile payload into a watt limit.

    Args:
        charging_profile: the `chargingProfile` object from the command.
        max_power_w: this station's hardware maximum. Requests above it
            are clamped, not refused -- a real charger physically cannot
            exceed its rating, and refusing would leave the station at
            its OLD limit, which is the opposite of what the operator
            asked for.
        evse_id: the `evseId` from the command, if present.
        station_evse_id: which EVSE this agent is. Matches
            messages.DEFAULT_EVSE_ID.

    Returns:
        ProfileLimit, ready to hand to Contract 5.

    Raises:
        ProfileRejected: and nothing else. Every internal failure is
            converted. See the module docstring.
    """
    # -- who is this for -------------------------------------------------
    #
    # evseId 0 means "the whole charging station" in OCPP 2.0.1, which
    # for a single-EVSE station is the same thing as evseId 1. Anything
    # else is addressed to hardware this station does not have, and
    # accepting it would mean silently applying a limit meant for a
    # different socket.
    if evse_id is not None:
        try:
            target = int(evse_id)
        except (TypeError, ValueError):
            raise ProfileRejected(f"evseId must be an integer, got {evse_id!r}")
        if target not in (0, station_evse_id):
            raise ProfileRejected(
                f"evseId {target} is not this station (it has EVSE "
                f"{station_evse_id}; 0 means the whole station)"
            )

    if not isinstance(charging_profile, dict):
        raise ProfileRejected("chargingProfile was missing or not an object")

    # -- purpose ---------------------------------------------------------
    purpose = _get(
        charging_profile, "charging_profile_purpose", "chargingProfilePurpose"
    )
    if purpose is not None and purpose not in SUPPORTED_PURPOSES:
        raise ProfileRejected(
            f"chargingProfilePurpose {purpose!r} is not supported by this "
            f"station (supported: {', '.join(SUPPORTED_PURPOSES)})"
        )

    # -- down to the schedule ---------------------------------------------
    schedule = _first_of_list(
        _get(charging_profile, "charging_schedule", "chargingSchedule"),
        "chargingSchedule",
    )

    unit = _get(schedule, "charging_rate_unit", "chargingRateUnit", default=UNIT_W)
    if unit not in CHARGING_RATE_UNITS:
        raise ProfileRejected(
            f"chargingRateUnit {unit!r} is not one of {CHARGING_RATE_UNITS}"
        )

    period = _first_of_list(
        _get(schedule, "charging_schedule_period", "chargingSchedulePeriod"),
        "chargingSchedulePeriod",
    )

    raw_limit = _as_float(
        _get(period, "limit", default=None), "chargingSchedulePeriod.limit"
    )

    phases_raw = _get(
        period, "number_phases", "numberPhases",
        default=_get(schedule, "number_phases", "numberPhases",
                     default=DEFAULT_NUMBER_PHASES),
    )
    try:
        number_phases = int(phases_raw)
    except (TypeError, ValueError):
        raise ProfileRejected(f"numberPhases must be an integer, got {phases_raw!r}")
    if number_phases not in (1, 2, 3):
        raise ProfileRejected(f"numberPhases must be 1, 2 or 3, got {number_phases}")

    # -- to watts -----------------------------------------------------------
    if unit == UNIT_A:
        watts = raw_limit * NOMINAL_VOLTAGE_V * number_phases
        LOG.info(
            "profile in amps: %.1fA x %.0fV x %d phase(s) = %.1fW "
            "(NOMINAL_VOLTAGE_V is an assumption -- see "
            "agent/charging_profile.py)",
            raw_limit, NOMINAL_VOLTAGE_V, number_phases, watts,
        )
    else:
        watts = raw_limit

    clamped = watts > max_power_w
    if clamped:
        LOG.warning(
            "requested limit %.1fW exceeds this station's maximum %.1fW; "
            "applying %.1fW. The CSMS believes this station can supply more "
            "than it can.", watts, max_power_w, max_power_w,
        )
        watts = max_power_w

    return ProfileLimit(
        watts=watts,
        raw_limit=raw_limit,
        unit=unit,
        number_phases=number_phases,
        clamped=clamped,
        profile_id=_get(charging_profile, "id", "charging_profile_id"),
        purpose=purpose,
        stack_level=_get(charging_profile, "stack_level", "stackLevel"),
    )


# -- the reverse direction: what ClearChargingProfile means -------------------


def cleared_limit_w(max_power_w: float) -> float:
    """
    The limit to return to when profiles are cleared.

    The station's own maximum. A cleared profile is not "zero power" --
    that mistake would stop every charging session on the fleet the
    moment an operator tidied up their profiles, which is the kind of
    thing that reads as a cryptography failure in an experiment log.

    A one-line function so the meaning has a name and one place to
    change, rather than `max_power_w` appearing bare at the call site
    where its intent is invisible.
    """
    return max_power_w