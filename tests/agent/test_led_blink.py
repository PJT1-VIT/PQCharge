"""
Runbook V2 (2026-10-14): the Raspberry Pi's LED blinks while the Pi is migrated.

  agent/gpio_power.py  GPIOPower.indicate_migration(): the LED blinks for
                       MIGRATION_BLINK_S, then shows what the contactor and the
                       power limit say (on while charging, dimmed when limited).
  agent/station.py     a successful RequestPQEnrolment (migration or key
                       rotation, never a boot check) calls it. A simulated
                       charger has no LED and nothing happens.

A fake LED records every value; a fake clock moves forward only when the blink
"sleeps", so no test waits in real time.
"""

from __future__ import annotations

import threading
import types

import pytest

from agent.gpio_power import MIGRATION_BLINK_PERIOD_S, MIGRATION_BLINK_S, GPIOPower


class _LED:
    def __init__(self, pin):
        self.pin, self.history = pin, []

    @property
    def value(self):
        return self.history[-1] if self.history else None

    @value.setter
    def value(self, v):
        self.history.append(v)


class _INA219:
    def power(self): return 20.0
    def current(self): return 6.0
    def voltage(self): return 3.3


class _Time:
    """A clock that only moves when the blink thread sleeps."""
    def __init__(self):
        self.t = 100.0
        self.on_sleep = None

    def clock(self):
        return self.t

    def sleep(self, s):
        self.t += s
        if self.on_sleep:
            self.on_sleep()


def _pi(tm):
    return GPIOPower(7400.0, led_factory=_LED, sensor_factory=lambda s, a: _INA219(),
                     clock=tm.clock, sleep=tm.sleep)


def _wait(p):
    p._blinker.join(5)
    assert not p._blinker.is_alive()


def test_blinks_then_goes_back_on_while_charging():
    tm = _Time()
    p = _pi(tm)
    p.close_contactor()                                    # charging: LED on (1.0)
    start = len(p._led.history)
    p.indicate_migration()
    _wait(p)
    blink = p._led.history[start:]
    assert blink[:4] == [1.0, 0.0, 1.0, 0.0]               # on/off/on/off ...
    assert blink[-1] == 1.0                                # ... then back on
    cycles = MIGRATION_BLINK_S / (MIGRATION_BLINK_PERIOD_S / 2)
    assert abs(len(blink) - 1 - cycles) <= 1               # about 3 s of half-periods


def test_blinks_at_full_brightness_and_ends_off_when_not_charging():
    tm = _Time()
    p = _pi(tm)
    start = len(p._led.history)
    p.indicate_migration(seconds=0.6)
    _wait(p)
    blink = p._led.history[start:]
    assert set(blink[:-1]) == {1.0, 0.0} and blink[-1] == 0.0


def test_a_limit_set_during_the_blink_is_shown_after_it():
    tm = _Time()
    p = _pi(tm)
    p.close_contactor()
    calls = {"n": 0}

    def limit_once():
        calls["n"] += 1
        if calls["n"] == 2:
            p.set_power_limit(3700)                        # curtailment arrives mid-blink
    tm.on_sleep = limit_once
    p.indicate_migration(seconds=1.2)
    _wait(p)
    assert p._led.value == 0.5                             # dimmed, as the limit says


def test_a_second_call_extends_the_same_blink():
    tm = _Time()
    gate = threading.Event()
    p = _pi(tm)

    def first_sleep():
        gate.wait(5)
    tm.on_sleep = first_sleep
    p.indicate_migration(seconds=0.3)
    first = p._blinker
    p.indicate_migration(seconds=3.0)                      # while blinking: same thread, longer
    assert p._blinker is first
    tm.on_sleep = None
    gate.set()
    _wait(p)
    assert tm.t >= 100.0 + 3.0


# -------------------------------------------------------------- station hook

def test_station_hook_calls_the_led_and_never_raises():
    from agent.station import ChargingStation

    called = []
    pi = types.SimpleNamespace(indicate_migration=lambda: called.append(1))
    log = types.SimpleNamespace(warning=lambda *a, **k: None)
    ChargingStation._indicate_migration(types.SimpleNamespace(power=pi, log=log))
    assert called == [1]

    sim = types.SimpleNamespace()                          # simulated charger: no LED
    ChargingStation._indicate_migration(types.SimpleNamespace(power=sim, log=log))

    warned = []
    def broken():
        raise OSError("GPIO busy")
    ChargingStation._indicate_migration(types.SimpleNamespace(
        power=types.SimpleNamespace(indicate_migration=broken),
        log=types.SimpleNamespace(warning=lambda *a, **k: warned.append(a))))
    assert warned                                          # logged, not raised


@pytest.mark.asyncio
async def test_a_real_enrolment_blinks_the_pi(tmp_path):
    pytest.importorskip("quantcrypt", reason="PQ backend; see requirements.txt")
    from agent import pqc_messages as pqcm
    from agent.client import StationClient
    from agent.config import AgentConfig
    from agent.station import ChargingStation

    st = ChargingStation(AgentConfig(station_id="CP0000", csms_url="ws://localhost:9000",
                                     pq_key_dir=str(tmp_path)))
    blinks = []
    st.power.indicate_migration = lambda: blinks.append(1)    # stand-in for the Pi's GPIOPower
    client = StationClient("CP0000", connection=None, commands=st)
    msg = pqcm.build_enrolment_request("CP0000")
    reply = await client.on_data_transfer(vendor_id=msg.vendor_id, message_id=msg.message_id, data=msg.data)
    assert reply.status == pqcm.STATUS_ACCEPTED
    assert blinks == [1]

    # A boot check (PQAuthChallenge) is NOT a migration: no blink.
    nonce = b"\x01" * 32
    ch = pqcm.build_challenge_message(nonce, key_id=st.pq.key_id)
    await client.on_data_transfer(vendor_id=ch.vendor_id, message_id=ch.message_id, data=ch.data)
    assert blinks == [1]
