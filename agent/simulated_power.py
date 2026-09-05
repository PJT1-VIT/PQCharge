"""
Software power backend. The only implementation for Review 3.

Energy accumulates in real time from the power draw, so a transaction
running for thirty seconds at 7.4 kW reports the watt-hours it would
actually have delivered. This matters because TransactionEvent meter
values feed the aggregate fleet power figure, and a counter that simply
increments would make that figure meaningless.
"""

from __future__ import annotations

import time

from agent.power import PowerInterface

DEFAULT_LIMIT_W = 7400.0
"""7.4 kW -- a typical single-phase AC charging station."""


class SimulatedPower(PowerInterface):
    """Computes power and energy rather than measuring them."""

    def __init__(self, max_power_w: float = DEFAULT_LIMIT_W) -> None:
        self._max_power_w = max_power_w
        self._limit_w = max_power_w
        self._closed = False
        self._energy_wh = 0.0
        self._last_tick = time.monotonic()

    def _accumulate(self) -> None:
        """
        Advance the energy counter to now.

        Called before every read and before every state change, so that
        energy always reflects the power draw that was actually in
        force over each interval -- including intervals where the limit
        changed partway through a transaction.
        """
        now = time.monotonic()
        elapsed_s = now - self._last_tick
        self._last_tick = now
        if self._closed and elapsed_s > 0:
            self._energy_wh += self._draw_w() * (elapsed_s / 3600.0)

    def _draw_w(self) -> float:
        """Power drawn while the contactor is closed."""
        return min(self._limit_w, self._max_power_w)

    def close_contactor(self) -> None:
        self._accumulate()
        self._closed = True

    def open_contactor(self) -> None:
        self._accumulate()
        self._closed = False

    def is_closed(self) -> bool:
        return self._closed

    def set_power_limit(self, watts: float) -> None:
        if watts < 0:
            raise ValueError(f"power limit must be non-negative, got {watts}")
        self._accumulate()
        self._limit_w = min(watts, self._max_power_w)

    def get_power_limit(self) -> float:
        return self._limit_w

    def read_power(self) -> float:
        self._accumulate()
        return self._draw_w() if self._closed else 0.0

    def read_energy(self) -> float:
        self._accumulate()
        return self._energy_wh

    def reset_meter(self) -> None:
        self._accumulate()
        self._energy_wh = 0.0