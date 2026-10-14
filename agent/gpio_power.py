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
        being migrated    -> LED blinks for MIGRATION_BLINK_S seconds, then
                             returns to the above (2026-10-14, runbook V2:
                             agent/station.py calls indicate_migration() when
                             a RequestPQEnrolment arrives -- migration or key
                             rotation, never a boot check)
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
import threading
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

MIGRATION_BLINK_S = 3.0
"""How long the LED blinks when the Pi is migrated or its key is rotated. A
real enrolment takes ~0.1 s -- far too short to see -- so the blink has a
fixed, visible length."""

MIGRATION_BLINK_PERIOD_S = 0.3
"""One on + off cycle of the blink."""


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
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(max_power_w=max_power_w)
        self._led = (led_factory or _default_led)(led_pin)
        self._clock = clock
        self._sleep = sleep
        self._blink_lock = threading.Lock()
        self._blink_until = float("-inf")
        self._blinker: threading.Thread | None = None
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
        if self.blinking:
            return                    # the blink thread restores the right value when it ends
        self._led.value = self.duty

    # -- runbook V2: the LED blinks while the Pi is being migrated ----------

    @property
    def blinking(self) -> bool:
        return self._clock() < self._blink_until

    def indicate_migration(self, seconds: float = MIGRATION_BLINK_S) -> None:
        """
        Blink the LED for `seconds` (called by agent/station.py when a
        RequestPQEnrolment arrives). A second call while blinking extends
        the blink. Returns at once: the blink runs on a small daemon
        thread, then sets the LED back to what the contactor and the power
        limit say -- including any limit changed during the blink.
        """
        with self._blink_lock:
            self._blink_until = max(self._blink_until, self._clock() + seconds)
            if self._blinker is not None and self._blinker.is_alive():
                return
            self._blinker = threading.Thread(target=self._blink, name="led-blink", daemon=True)
            self._blinker.start()
        LOG.info("LED blinking: this charger is being migrated (%.1f s)", seconds)

    def _blink(self) -> None:
        on = False
        try:
            while self.blinking:
                on = not on
                # Blink at the charging brightness; full brightness if not charging.
                self._led.value = (self.duty or 1.0) if on else 0.0
                self._sleep(MIGRATION_BLINK_PERIOD_S / 2)
        finally:
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

    def meter_custom_data(self) -> dict[str, Any] | None:
        """
        L37 (B-F10): the physical reading for the server, sent as OCPP
        customData on the power sample of every live meter value. Keys are
        snake_case; the ocpp library sends them as camelCase (vendorId,
        measuredMw, ...). None when the sensor gives no reading.
        """
        raw = self.raw_reading()
        if raw["mw"] is None:
            return None
        data = {
            "vendor_id": "pqcharge.hw",
            "measured_mw": round(raw["mw"], 2),
            "measured_ma": round(raw["ma"], 3) if raw["ma"] is not None else None,
            "measured_v": round(raw["v"], 3) if raw["v"] is not None else None,
            "led_duty": round(self.duty, 3),
            "power_basis": "scaled",
            "sensor": "INA219",
        }
        return {k: v for k, v in data.items() if v is not None}

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


def _selftest() -> None:  # pragma: no cover - needs the real Pi
    """
    python -m agent.gpio_power  -- on the Pi, no server needed (B-F9).

    Steps the LED through off / 25 % / 50 % / 100 % / off and prints the
    INA219 reading at each step, so the wiring can be checked before the
    charger joins the fleet: the milliwatts must rise with the brightness.
    """
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        power = GPIOPower()
    except GPIOPowerUnavailable as exc:
        print(exc)
        sys.exit(1)
    power.close_contactor()
    for watts in (0.0, 1850.0, 3700.0, 7400.0):
        power.set_power_limit(watts)
        time.sleep(1.5)
        raw = power.raw_reading()
        print(f"limit {watts:6.0f} W -> LED duty {power.duty:4.0%} -> "
              f"INA219 {raw['mw']} mW, {raw['ma']} mA, {raw['v']} V")
    power.open_contactor()
    power.close()
    print("LED off. If the mW did not rise with the duty, check the wiring and i2cdetect -y 1.")


if __name__ == "__main__":  # pragma: no cover
    _selftest()
