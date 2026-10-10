"""B-F8: the Raspberry Pi's GPIOPower, with a fake LED and a fake INA219."""

import pytest

from agent.config import AgentConfig
from agent.gpio_power import GPIOPower, GPIOPowerUnavailable, build_power
from agent.simulated_power import SimulatedPower


class _LED:
    def __init__(self, pin):
        self.pin, self.value = pin, None


class _INA219:
    def power(self): return 23.4
    def current(self): return 7.1
    def voltage(self): return 3.29


def _gpio(**kw):
    return GPIOPower(7400.0, led_factory=_LED, sensor_factory=lambda s, a: _INA219(), **kw)


def test_led_follows_contactor_and_limit():
    p = _gpio()
    assert p._led.pin == 18 and p._led.value == 0.0          # off at start
    p.close_contactor()
    assert p._led.value == 1.0 and p.read_power() == 7400.0   # full power
    p.set_power_limit(3700)
    assert p._led.value == 0.5 and p.read_power() == 3700.0   # curtailed: dimmed, scaled W
    p.set_power_limit(0)
    assert p._led.value == 0.0 and p.is_closed()              # throttled to standstill
    p.set_power_limit(7400)
    p.open_contactor()
    assert p._led.value == 0.0 and p.read_power() == 0.0


def test_raw_reading_comes_from_the_sensor():
    assert _gpio().raw_reading() == {"mw": 23.4, "ma": 7.1, "v": 3.29}


def test_missing_sensor_does_not_stop_the_charger():
    def broken(s, a):
        raise OSError("no I2C device at 0x40")

    p = GPIOPower(7400.0, led_factory=_LED, sensor_factory=broken)
    p.close_contactor()
    assert p._led.value == 1.0 and p.read_power() == 7400.0
    assert p.raw_reading() == {"mw": None, "ma": None, "v": None}


def test_cli_selects_the_backend():
    import argparse

    parser = argparse.ArgumentParser()
    AgentConfig.add_arguments(parser)
    cfg = AgentConfig.from_namespace(parser.parse_args(["--power", "gpio", "--led-pin", "13"]))
    assert cfg.power_backend == "gpio" and cfg.led_pin == 13
    assert AgentConfig.from_namespace(parser.parse_args([])).power_backend == "sim"
    assert type(build_power(AgentConfig())) is SimulatedPower


def test_off_the_pi_it_says_so():
    try:
        import gpiozero  # noqa: F401
        pytest.skip("gpiozero installed here")
    except ImportError:
        with pytest.raises(GPIOPowerUnavailable, match="--power sim"):
            GPIOPower(7400.0, sensor_factory=lambda s, a: _INA219())
