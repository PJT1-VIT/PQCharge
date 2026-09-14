"""
The station's physical state — one source of truth for two OCPP fields.

Track C (agent). Phase C3.

--------------------------------------------------------------------
WHERE THIS FITS

    agent/messages.py        the wire strings
          |
    agent/state_machine.py <- you are here. Pure logic. No network,
          |                   no asyncio, no ocpp import, no power.
    agent/station.py          owns ONE instance, asks it questions,
                              and sends whatever it answers.

--------------------------------------------------------------------
WHY THIS FILE EXISTS AT ALL

Up to Phase C2 the agent wrote two OCPP fields at separate call sites:

    connector_status   in StatusNotification   ("Available"/"Occupied")
    charging_state     in TransactionEvent     ("Charging"/"Idle")

Nothing tied them together. It was entirely possible -- and, once a
charging profile can arrive mid-session, likely -- to report
connector_status "Occupied" while charging_state still said "Charging"
after the power had been cut to zero. Track A stores both without
reconciling them, so the dashboard would show a station charging at 0 W
and nobody would know which field was lying.

So: ONE internal state. Both wire fields are DERIVED from it. It is
impossible for them to disagree, because there is only one thing to
disagree with.

--------------------------------------------------------------------
THE STATES, AND WHAT EACH ONE MEANS PHYSICALLY

    AVAILABLE        nothing plugged in, contactor open
    OCCUPIED         car plugged in, no transaction yet, no current
    CHARGING         transaction open, contactor closed, current flowing
    SUSPENDED_EV     the CAR asked to stop drawing (battery full, or its
                     own thermal limit). Transaction stays open.
    SUSPENDED_EVSE   WE stopped supplying. Transaction stays open.
                     *** THIS IS THE CURTAILMENT STATE. ***
                     A charging profile of 0 W lands here. It is the
                     demo moment for E5: the CSMS sends one command and
                     a station's power drops to zero without the
                     session ending.
    FAULTED          something is wrong. Contactor open, no charging.

SUSPENDED_EV and SUSPENDED_EVSE look identical on a dashboard and are
completely different events. One is the car's decision, one is the
operator's. Reporting the wrong one would make a deliberate curtailment
look like an ordinary full battery in the results.

--------------------------------------------------------------------
NOTE ON TRANSITIONS BEING ENFORCED

An illegal transition raises. It is a programming error, not a runtime
condition -- there is no sequence of server commands that can request
one, because station.py maps every command onto a legal target. Failing
loudly here means such a bug surfaces in a unit test rather than as a
station that reports "Available" while current is flowing.

The one exception is fault(), which is reachable from anywhere. A
safety stop must never be blocked by a transition table.
--------------------------------------------------------------------
"""

from __future__ import annotations

from enum import Enum
from typing import Callable

from agent import messages as msg
from agent.logging_setup import get_logger


class StationState(str, Enum):
    """
    The agent's own state. INTERNAL -- never sent on the wire.

    Inherits from str so that log formatting and dict keys are painless,
    but the VALUES here are our own names, not OCPP's. The OCPP strings
    come from the mapping below and from agent/messages.py, which is the
    only place wire values are allowed to live.
    """

    AVAILABLE = "available"
    OCCUPIED = "occupied"
    CHARGING = "charging"
    SUSPENDED_EV = "suspended_ev"
    SUSPENDED_EVSE = "suspended_evse"
    FAULTED = "faulted"

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value


# -- the mapping table: one state -> both wire fields --------------------
#
# Read this table and you know exactly what the CSMS will see in every
# state. tests/agent/test_state_machine.py asserts on it field by field,
# because these strings are the contract with csms/handlers.py and
# csms/registry.py, and a typo here is a silent data error rather than a
# crash.
#
#   connector_status  -> StatusNotification.connector_status
#   charging_state    -> TransactionEvent.transaction_info.charging_state
#                        None means "no transaction is open, so this
#                        field is not sent at all".
#   draws_power       -> whether the contactor should be closed AND the
#                        limit non-zero. station.py actuates from this.

_MAPPING: dict[StationState, tuple[str, str | None, bool]] = {
    #                        connector_status         charging_state                      draws_power
    StationState.AVAILABLE:      (msg.STATUS_AVAILABLE, None,                                False),
    # OCCUPIED reports no charging_state, because in this agent's model
    # a transaction does not exist yet (before Authorize) or no longer
    # exists (after the Ended event). OCPP 2.0.1 does allow a
    # transaction to be open in an EVConnected state -- a cable plugged
    # in before the driver has presented a card -- but this agent does
    # not start a transaction until it is authorised, so reporting a
    # charging_state here would mean sending a field for a transaction
    # that has no id. messages.CHARGING_STATE_EV_CONNECTED stays defined
    # for when that flow is added.
    StationState.OCCUPIED:       (msg.STATUS_OCCUPIED,  None,                                False),
    StationState.CHARGING:       (msg.STATUS_OCCUPIED,  msg.CHARGING_STATE_CHARGING,         True),
    StationState.SUSPENDED_EV:   (msg.STATUS_OCCUPIED,  msg.CHARGING_STATE_SUSPENDED_EV,     False),
    StationState.SUSPENDED_EVSE: (msg.STATUS_OCCUPIED,  msg.CHARGING_STATE_SUSPENDED_EVSE,   False),
    StationState.FAULTED:        (msg.STATUS_FAULTED,   None,                                False),
}


# -- what may follow what ------------------------------------------------
#
# FAULTED is reachable from everywhere via fault() and is therefore not
# repeated in every row. A self-transition (X -> X) is always allowed and
# is a no-op; see transition_to().

_ALLOWED: dict[StationState, frozenset[StationState]] = {
    # A car arrives. Nothing else can happen from empty.
    StationState.AVAILABLE: frozenset({
        StationState.OCCUPIED,
    }),
    # Plugged in: either current starts, or the driver leaves again, or
    # the session begins already suspended (a 0 W profile that arrived
    # before the transaction did).
    StationState.OCCUPIED: frozenset({
        StationState.CHARGING,
        StationState.SUSPENDED_EV,
        StationState.SUSPENDED_EVSE,
        StationState.AVAILABLE,
    }),
    # Charging can be suspended by either side, end, or be unplugged.
    StationState.CHARGING: frozenset({
        StationState.SUSPENDED_EV,
        StationState.SUSPENDED_EVSE,
        StationState.OCCUPIED,
        StationState.AVAILABLE,
    }),
    # A suspension can lift, swap cause, or end the session.
    StationState.SUSPENDED_EV: frozenset({
        StationState.CHARGING,
        StationState.SUSPENDED_EVSE,
        StationState.OCCUPIED,
        StationState.AVAILABLE,
    }),
    StationState.SUSPENDED_EVSE: frozenset({
        StationState.CHARGING,
        StationState.SUSPENDED_EV,
        StationState.OCCUPIED,
        StationState.AVAILABLE,
    }),
    # Recovery from a fault returns to empty and nowhere else. A station
    # that faulted mid-transaction has lost the transaction; pretending
    # otherwise would report energy figures across a gap.
    StationState.FAULTED: frozenset({
        StationState.AVAILABLE,
    }),
}


# States in which a transaction is open, so charging_state must be sent.
TRANSACTION_STATES = frozenset({
    StationState.CHARGING,
    StationState.SUSPENDED_EV,
    StationState.SUSPENDED_EVSE,
})


class IllegalTransition(ValueError):
    """
    A transition the table forbids.

    A ValueError subclass so that a caller which only guards against
    ValueError still catches it, and so it reads as "bad argument"
    rather than "the station broke" -- which is accurate: it means the
    caller asked for something impossible.
    """

    def __init__(self, current: StationState, target: StationState) -> None:
        super().__init__(
            f"illegal transition {current.value} -> {target.value}; "
            f"allowed from {current.value}: "
            f"{sorted(s.value for s in _ALLOWED[current])} (plus fault())"
        )
        self.current = current
        self.target = target


class StationStateMachine:
    """
    One station's physical state.

    STATION-SCOPED, like the power backend and the transaction id: it
    lives in agent/station.py and survives a dropped connection. That is
    deliberate and it matters in Phase C4 -- a station whose socket died
    mid-charge is still physically charging, and must resume reporting
    the state it is actually in rather than starting again from
    AVAILABLE.

    Holds no asyncio primitives, so it can be unit tested with no event
    loop at all.
    """

    def __init__(
        self,
        station_id: str = "",
        initial: StationState = StationState.AVAILABLE,
        on_change: Callable[[StationState, StationState, str], None] | None = None,
    ) -> None:
        """
        Args:
            station_id: for the log prefix only.
            initial: almost always AVAILABLE. Overridable so a test can
                start mid-session without driving it there first.
            on_change: called after every ACTUAL change with
                (previous, current, reason). Synchronous by design --
                see the note in agent/client.py about why nothing
                reachable from an OCPP handler may await.
        """
        self._state = initial
        self._on_change = on_change
        self.log = get_logger(__name__, station_id=station_id)

        self.transition_count = 0
        """How many real changes have happened. Surfaced in the
        end-of-run summary: a session that never left AVAILABLE is a
        session that did nothing, and a count is how that is noticed
        without reading the whole log."""

    # -- reading the state -------------------------------------------------

    @property
    def state(self) -> StationState:
        return self._state

    @property
    def connector_status(self) -> str:
        """What StatusNotification should carry right now."""
        return _MAPPING[self._state][0]

    @property
    def charging_state(self) -> str | None:
        """
        What transaction_info.charging_state should carry right now, or
        None when no transaction is open and the field must be omitted.
        """
        return _MAPPING[self._state][1]

    @property
    def draws_power(self) -> bool:
        """
        Whether current should be flowing.

        station.py actuates the contactor from this rather than from a
        separate flag, so there is no way for the state and the physical
        output to disagree.
        """
        return _MAPPING[self._state][2]

    @property
    def in_transaction(self) -> bool:
        """Whether a transaction is open in this state."""
        return self._state in TRANSACTION_STATES

    # -- changing the state -------------------------------------------------

    def can(self, target: StationState) -> bool:
        """
        Whether transition_to(target) would succeed.

        Used by the OCPP handlers to answer Accepted/Rejected BEFORE
        changing anything -- a command that cannot be honoured must be
        refused on the wire, not accepted and then ignored.
        """
        if target is self._state:
            return True
        if target is StationState.FAULTED:
            return True  # fault() is always reachable
        return target in _ALLOWED[self._state]

    def transition_to(self, target: StationState, reason: str) -> bool:
        """
        Move to `target`.

        Returns:
            True if the state actually changed, False if it was already
            `target`. The return value is what station.py uses to decide
            whether a StatusNotification is worth sending -- re-sending
            an identical status is harmless to Track A, which guards
            against it, but at five hundred stations those messages add
            up on a server the E2 storm is deliberately overloading.

        Raises:
            IllegalTransition: the table forbids it. See the module
                docstring for why this is loud rather than lenient.
        """
        if target is self._state:
            self.log.debug("state already %s (%s)", target.value, reason)
            return False

        if target is not StationState.FAULTED and target not in _ALLOWED[self._state]:
            raise IllegalTransition(self._state, target)

        return self._set(target, reason)

    def fault(self, reason: str) -> bool:
        """
        Enter FAULTED from anywhere.

        Separate from transition_to() on purpose: a safety stop must not
        be refusable by a transition table. Whatever state the station
        believes it is in, it can always declare itself broken.
        """
        return self._set(StationState.FAULTED, reason)

    def reset(self, reason: str = "reset") -> bool:
        """
        Return to AVAILABLE from anywhere.

        Used on session teardown and on fault recovery. Like fault(),
        deliberately unconstrained -- "nothing is plugged in" is always
        a physically reachable truth, and a station that could not get
        back to it would be stuck forever after one bad sequence.
        """
        return self._set(StationState.AVAILABLE, reason)

    def _set(self, target: StationState, reason: str) -> bool:
        """The single place the state is actually written."""
        if target is self._state:
            return False

        previous = self._state
        self._state = target
        self.transition_count += 1

        # ONE INFO LINE PER TRANSITION, with the reason. This is the
        # spine of C3's debugging story: reading the agent log top to
        # bottom shows the whole physical history of the station, and
        # every line says what caused it. A transition without a reason
        # is a transition nobody can explain three weeks later when the
        # results look wrong.
        self.log.info(
            "state %s -> %s (%s) | connector=%s charging_state=%s power=%s",
            previous.value,
            target.value,
            reason,
            self.connector_status,
            self.charging_state or "-",
            "on" if self.draws_power else "off",
        )

        if self._on_change is not None:
            # Deliberately not wrapped in try/except. A callback that
            # raises is a bug in station.py, and swallowing it here
            # would leave the state machine and the station's idea of
            # the world silently out of step.
            self._on_change(previous, target, reason)

        return True

    # -- diagnostics ----------------------------------------------------------

    def describe(self) -> str:
        """One-line summary for a log or a test failure message."""
        return (
            f"{self._state.value} "
            f"(connector={self.connector_status}, "
            f"charging_state={self.charging_state or '-'}, "
            f"power={'on' if self.draws_power else 'off'})"
        )

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"StationStateMachine({self.describe()})"