"""
Meter value interpretation — turning OCPP sampled values into numbers.

Track A (csms). Phase A3.

--------------------------------------------------------------------
WHY THIS IS ITS OWN MODULE, AND WHY IT WARNS INSTEAD OF IGNORING

A TransactionEvent carries a list of meter values, each holding a list
of sampled values. Every sample is tagged with a *measurand* -- a label
saying what kind of measurement it is -- and a unit. The CSMS reads the
label to decide which sample is instantaneous power and which is
cumulative energy.

The failure this module exists to prevent: the agent sends one label,
the server looks for another, the server silently ignores every sample,
power_w stays None, aggregate_power_w stays 0.0, and the dashboard
shows a fleet that is charging nothing. Nothing crashes. It is
discovered at demo rehearsal, because the fleet-wide power spike is the
entire visible payload of experiment E5.

So this module never ignores a sample quietly. An unrecognised
measurand is returned in `unrecognised` and the caller logs it by name.
A mismatch becomes a warning on day one rather than a mystery on day
thirteen.

It also converts units rather than assuming them. A station reporting
kilowatts instead of watts would otherwise make every figure in the
results 1000x wrong in a direction nobody would question.

Pure functions, no OCPP library import, no I/O -- so it is unit
testable without a running server.
--------------------------------------------------------------------

Provisional agreement with Track C, recorded in
claude/TrackA_Dev_Plan.md. Track A chose these; Track C's agent must
match them, and the warning path exists because it might not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

POWER_MEASURANDS: frozenset[str] = frozenset({"power.active.import"})
"""Instantaneous power drawn by the station, in watts."""

ENERGY_MEASURANDS: frozenset[str] = frozenset(
    {"energy.active.import.register"}
)
"""Cumulative energy delivered, in watt-hours."""

DEFAULT_MEASURAND = "energy.active.import.register"
"""OCPP 2.0.1 says a sampled value with no measurand is cumulative
import energy. Applied rather than treated as unrecognised, because a
conformant station is entitled to omit the field."""

_UNIT_SCALE_TO_BASE: dict[str, float] = {
    "": 1.0,
    "w": 1.0,
    "kw": 1000.0,
    "wh": 1.0,
    "kwh": 1000.0,
}
"""Scale factor to reach watts or watt-hours, which are Contract 5's
units. An unknown unit is treated as already-base and reported, rather
than guessed at."""


@dataclass
class MeterReading:
    """What one TransactionEvent's meter values amounted to."""

    power_w: float | None = None
    """Instantaneous draw in watts, if any sample carried it."""

    energy_wh: float | None = None
    """Cumulative energy in watt-hours, if any sample carried it."""

    reading_at: datetime | None = None
    """The station's own timestamp for the most recent meter value.

    Used by the registry to reject stale readings: a station that
    queued events while offline replays them on reconnect, and an old
    reading must not overwrite a newer live one. See
    SessionRegistry.record_meter.
    """

    unrecognised: list[str] = field(default_factory=list)
    """Measurand labels that were neither power nor energy. Logged by
    name so a mismatch with Track C's agent is loud and immediate."""

    unknown_units: list[str] = field(default_factory=list)
    """Units that could not be scaled to watts or watt-hours."""

    sample_count: int = 0
    """How many sampled values were seen, recognised or not. A reading
    with samples but no recognised values is the signature of a
    measurand mismatch."""


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO-8601 string, tolerating a trailing Z."""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _scaled_value(sample: dict[str, Any]) -> tuple[float | None, str | None]:
    """
    Convert one sampled value to its base unit.

    Returns:
        (value in watts or watt-hours, unrecognised unit or None)

    OCPP's unit_of_measure carries an optional `multiplier`, a power of
    ten applied to the value. Honoured here because a station reporting
    7.4 with multiplier 3 means 7400, and dropping it would be a
    thousand-fold error that looks plausible.
    """
    try:
        raw = float(sample.get("value"))
    except (TypeError, ValueError):
        return None, None

    unit_block = sample.get("unit_of_measure") or {}
    unit = str(unit_block.get("unit", "") or "").strip().lower()
    try:
        multiplier = int(unit_block.get("multiplier", 0) or 0)
    except (TypeError, ValueError):
        multiplier = 0

    raw *= 10.0 ** multiplier

    if unit not in _UNIT_SCALE_TO_BASE:
        return raw, unit
    return raw * _UNIT_SCALE_TO_BASE[unit], None


def parse_meter_values(meter_values: Any) -> MeterReading:
    """
    Reduce a TransactionEvent's meter_value list to one reading.

    Later meter values win, because the list is chronological and the
    last entry is the most recent state of the meter.

    Malformed entries are skipped rather than raised on. A meter value
    that cannot be parsed must not take down the connection handler and
    disconnect a charging station -- the sample is lost, the session is
    not.
    """
    reading = MeterReading()
    if not isinstance(meter_values, (list, tuple)):
        return reading

    for meter_value in meter_values:
        if not isinstance(meter_value, dict):
            continue

        timestamp = _parse_timestamp(meter_value.get("timestamp"))
        if timestamp is not None and (
            reading.reading_at is None or timestamp >= reading.reading_at
        ):
            reading.reading_at = timestamp

        for sample in meter_value.get("sampled_value") or []:
            if not isinstance(sample, dict):
                continue
            reading.sample_count += 1

            measurand = str(
                sample.get("measurand") or DEFAULT_MEASURAND
            ).strip().lower()

            value, unknown_unit = _scaled_value(sample)
            if unknown_unit and unknown_unit not in reading.unknown_units:
                reading.unknown_units.append(unknown_unit)
            if value is None:
                continue

            if measurand in POWER_MEASURANDS:
                reading.power_w = value
            elif measurand in ENERGY_MEASURANDS:
                reading.energy_wh = value
            elif measurand not in reading.unrecognised:
                reading.unrecognised.append(measurand)

    return reading
