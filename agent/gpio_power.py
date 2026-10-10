"""
GPIOPower -- the Raspberry Pi charger's power backend (Contract 5).

Track B, B-F8 (2026-10-10). The Pi software was moved from Track C (C9) to
Track B for the final two days; this file is the agreed exception inside
agent/ (see the FINAL 2-DAY PLAN in the dev plans).

THE HARDWARE (wired by the team, out of scope here)
  - an LED with a 330 ohm resistor on GPIO18 (hardware PWM pin). It stands
    for the charger's output:
        contactor closed  -> LED on
        power limit       -> LED brightness (PWM duty = limit / max power)
        contactor open    -> LED off
  - an INA219 current/voltage sensor on I2C (address 0x40 by default),
    measuring the LED circuit -- tens of milliwatts.

WHAT IT REPORTS (L32, decided in the FINAL 2-DAY PLAN: "scaled")
  read_power() returns the SCALED power, duty x max power (7.4 kW by
  default), so the Pi sits naturally next to the simulated chargers in the
  fleet charts and in E5's curtailment chart. Energy is integrated from that
  scaled power, exactly as SimulatedPower does -- this class IS a
  SimulatedPower that also drives the LED and reads the sensor.
  The REAL measurement is the INA219's milliwatts: raw_reading() returns it,
  and it is logged every few seconds on the Pi's console, so the panel can
  watch the physical reading change when the fleet is curtailed. It is never
  presented as a measured 7.4 kW.

ROBUST BY DESIGN FOR A LIVE DEMO
  - No sensor (not wired, wrong address, I2C off): the LED and the charger
    still work; the reading is reported as unavailable and a warning is
    logged once.
  - Not on a Pi (gpiozero missing): construction fails with a clear message
    telling the user to run with --power sim.

Libraries (only on the Pi): `pip install gpiozero pi-ina219` (on Raspberry
Pi OS, gpiozero is preinstalled system-wide; inside a venv install it, or
create the venv with --system-site-packages). I2C must be enabled
(`sudo raspi-config` -> Interface Options -> I2C).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from agent.simulated_power import DEFAULT_LIMIT_W, SimulatedPower

LOG = logging.getLogger("agent.gpio_power")

DEFAULT_LED_PIN = 18
"""BCM numbering. GPIO18 = physical pin 12, a hardware-PWM pin."""

DEFAULT_SHUNT_OHMS = 0.1
"""The shunt resistor on the common INA219 breakout boards."""

DEFAULT_INA219_ADDRESS = 0x40

LOG_EVERY_S = 5.0
"""How often the raw INA219 reading is logged on the Pi's console."""


class GPIOPowerUnavailable(RuntimeError):
    """The GPIO library is not available: not a Raspberry Pi, or not installed."""


def _default_led(pin: int) -> Any:
    try:
        from gpiozero import PWMLED  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - only off the Pi
        raise GPIOPowerUnavailable(
            "gpiozero is not installed -- --power gpio only works on the "
            "Raspberry Pi (pip install gpiozero pi-ina219). On a laptop use "
            "--power sim."
        ) from exc
    return PWMLED(pin)


def _default_sensor(shunt_ohms: float, address: int) -> Any:
    from ina219 import INA219  # type: ignore[import-not-found]

    sensor = INA219(shunt_ohms, address=address)
    sensor.configure()
    return sensor


class GPIOPower(SimulatedPower):
    """Contract 5 on real hardware: drives the LED, reads the INA219."""

    def __init__(
        self,
        max_power_w: float = DEFAULT_LIMIT_W,
        *,
        led_pin: int = DEFAULT_LED_PIN,
        shunt_ohms: float = DEFAULT_SHUNT_OHMS,
        ina219_address: int = DEFAULT_INA219_ADDRESS,
        led_factory: Callable[[int], Any] | None = None,
        sensor_factory: Callable[[float, int], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(max_power_w=max_power_w)
        self._led = (led_factory or _default_led)(led_pin)
        self._clock = clock
        self._last_log = float("-inf")
        try:
            self._sensor = (sensor_factory or _default_sensor)(shunt_ohms, ina219_address)
        except Exception as exc:  # noqa: BLE001 - the LED must still work
            LOG.warning("INA219 not available (%s: %s) -- the LED still works, "
                        "but no physical reading will be shown", type(exc).__name__, exc)
            self._sensor = None
        self._apply_led()
        LOG.info("GPIOPower ready: LED on GPIO%d, INA219 %s, max %.0f W (scaled)",
                 led_pin, "found" if self._sensor is not None else "MISSING", max_power_w)

    # -- the LED follows the contactor and the limit ----------------------

    @property
    def duty(self) -> float:
        """LED brightness 0..1 = the share of max power currently allowed."""
        if not self._closed or self._max_power_w <= 0:
            return 0.0
        return max(0.0, min(1.0, self._draw_w() / self._max_power_w))

    def _apply_led(self) -> None:
        self._led.value = self.duty

    def close_contactor(self) -> None:
        super().close_contactor()
        self._apply_led()

    def open_contactor(self) -> None:
        super().open_contactor()
        self._apply_led()

    def set_power_limit(self, watts: float) -> None:
        super().set_power_limit(watts)
        self._apply_led()

    # -- readings -----------------------------------------------------------

    def raw_reading(self) -> dict[str, float | None]:
        """The INA219's physical reading: milliwatts, milliamps, volts."""
        if self._sensor is None:
            return {"mw": None, "ma": None, "v": None}
        try:
            return {"mw": float(self._sensor.power()),
                    "ma": float(self._sensor.current()),
                    "v": float(self._sensor.voltage())}
        except Exception as exc:  # noqa: BLE001 - a bad read never stops charging
            LOG.warning("INA219 read failed: %s: %s", type(exc).__name__, exc)
            return {"mw": None, "ma": None, "v": None}

    def read_power(self) -> float:
        """Scaled watts (L32). Also logs the physical reading every few s."""
        watts = super().read_power()
        now = self._clock()
        if now - self._last_log >= LOG_EVERY_S:
            self._last_log = now
            raw = self.raw_reading()
            if raw["mw"] is None:
                LOG.info("LED duty %3.0f%% -> reported %.0f W (scaled); INA219: no reading",
                         self.duty * 100, watts)
            else:
                LOG.info("LED duty %3.0f%% -> reported %.0f W (scaled); INA219 measured "
                         "%.1f mW (%.2f mA at %.2f V)",
                         self.duty * 100, watts, raw["mw"], raw["ma"], raw["v"])
        return watts

    def close(self) -> None:
        """Switch the LED off (end of run)."""
        try:
            self._led.value = 0.0
            closer = getattr(self._led, "close", None)
            if callable(closer):
                closer()
        except Exception:  # noqa: BLE001 - shutting down
            pass


def build_power(config: Any) -> SimulatedPower:
    """The Contract 5 backend named by config.power_backend ("sim" | "gpio")."""
    backend = getattr(config, "power_backend", "sim")
    if backend == "gpio":
        return GPIOPower(
            max_power_w=config.max_power_w,
            led_pin=getattr(config, "led_pin", DEFAULT_LED_PIN),
            shunt_ohms=getattr(config, "ina219_shunt_ohms", DEFAULT_SHUNT_OHMS),
        )
    return SimulatedPower(max_power_w=config.max_power_w)
