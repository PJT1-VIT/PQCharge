"""
Contract 5 — Power interface.

Provided and consumed by:  Track C (agent)

FROZEN INTERFACE. Signatures agreed on Day 2.

Two implementations behind one interface: SimulatedPower returns
computed values, GPIOPower drives a relay and reads a current sensor.
Selected by configuration.

This contract exists even though a single track owns both sides,
because it is what makes the hardware bench node additive rather than
structural. The agent, the CSMS, the ID manager and the status page
never learn whether a node is physical. Adding the Raspberry Pi later
means writing one subclass and changing one config value -- not
forking the agent.

Only SimulatedPower is implemented for Review 3. GPIOPower arrives in
the Review 4 window.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class PowerInterface(ABC):
    """
    The agent's view of its own power hardware.

    The agent calls these; it never touches GPIO or a sensor directly.
    All energy is in watt-hours, all power in watts, matching the units
    OCPP TransactionEvent meter values expect.
    """

    @abstractmethod
    def close_contactor(self) -> None:
        """
        Close the contactor and allow current to flow. Called when a
        transaction starts.

        Idempotent: closing an already-closed contactor is not an error.
        """

    @abstractmethod
    def open_contactor(self) -> None:
        """
        Open the contactor and stop current. Called when a transaction
        ends, and on RemoteStopTransaction.

        Idempotent. Must be safe to call from an error path -- this is
        the operation that stops power when something has gone wrong.
        """

    @abstractmethod
    def is_closed(self) -> bool:
        """Whether the contactor is currently closed."""

    @abstractmethod
    def set_power_limit(self, watts: float) -> None:
        """
        Cap the power the station may draw. Called on SetChargingProfile
        -- the actuation path that makes this a cyber-physical system.

        A limit of zero means draw nothing without opening the
        contactor, which is how a charging profile throttles a station
        to standstill without ending its transaction.
        """

    @abstractmethod
    def get_power_limit(self) -> float:
        """The current cap in watts."""

    @abstractmethod
    def read_power(self) -> float:
        """
        Instantaneous power draw in watts, now.

        Simulated: derived from the limit and contactor state.
        Hardware: computed from the sensor's voltage and current
        readings. Either way the agent treats it as a measurement.
        """

    @abstractmethod
    def read_energy(self) -> float:
        """
        Cumulative energy delivered in watt-hours since the meter was
        last reset.

        Monotonic non-decreasing, as a real meter is. It never resets
        on its own -- reset_meter() is explicit, because a meter that
        silently returns to zero produces billing disputes in the real
        world and unusable measurements here.
        """

    @abstractmethod
    def reset_meter(self) -> None:
        """Reset cumulative energy to zero. Called between transactions."""