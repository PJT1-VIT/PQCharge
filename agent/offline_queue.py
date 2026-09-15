"""
Events generated while the CSMS was unreachable, waiting to be replayed.

Track C (agent). Phase C4.

--------------------------------------------------------------------
WHERE THIS FITS

    agent/station.py       appends while disconnected, drains on rejoin
          |
    agent/offline_queue.py <- you are here. A bounded FIFO. No asyncio,
          |                   no network, no clock beyond what it is
          |                   handed.
    agent/client.py           sends each drained event with offline=True

--------------------------------------------------------------------
WHY A QUEUE AT ALL

A station charging when the CSMS dies is still charging. Current is
still flowing, energy is still accumulating, and the car does not care
that a server went away. Those readings happened. Throwing them away
would mean every E2 run reports a gap in the energy curve exactly as
wide as the outage -- and the analysis at Stage 9 could not tell that
gap apart from a station that genuinely stopped charging.

So the readings are kept, and replayed when the station rejoins, each
one marked offline=True and carrying ITS ORIGINAL TIMESTAMP.

--------------------------------------------------------------------
*** THE ORIGINAL TIMESTAMP IS THE WHOLE POINT ***

A replayed event must carry the time the reading was TAKEN, not the
time it was sent. Two reasons, and the second one is not obvious:

1. Otherwise the energy curve shows forty seconds of readings all
   stamped at the same instant, which is not what happened.

2. csms/registry.py has a last_meter_at staleness guard whose entire
   job is to stop a replayed backlog overwriting newer live state --
   Track A's comment says aggregate_power_w "would jump backwards on
   the dashboard at exactly the moment the fleet is being watched
   recover". That guard compares the incoming timestamp against the
   last one applied. If replays carried now(), every replayed event
   would look newer than everything, the guard would never fire, and
   Track A's defence against this exact problem would be silently
   disabled by us.

That is why QueuedEvent stores a timestamp at all, rather than letting
client.py stamp it at send time as it does for live events.

--------------------------------------------------------------------
WHY IT IS BOUNDED

Five hundred agents, a reading every five seconds, a two-minute outage:
twelve thousand queued objects. That is survivable. A ten-minute outage
during an unattended overnight run is sixty thousand, and an agent that
queues without limit turns a server outage into an out-of-memory kill
of the load generator -- which would end the run, destroy the
measurement, and look like a crash rather than a capacity limit.

So the queue has a cap and drops the OLDEST when full, keeping the
readings nearest the reconnection. And it COUNTS what it dropped.

A run with dropped events has holes in its dataset. That is acceptable;
what is not acceptable is not knowing. The count is logged at WARNING
on every drop boundary and reported in the end-of-run summary, so the
holes are a documented limitation rather than an anomaly somebody
discovers at Stage 9 and cannot explain.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from agent.logging_setup import get_logger

DEFAULT_MAX_EVENTS = 2000
"""
Roughly three hours of one station metering every five seconds.

Generous for a single agent and deliberately so -- the cap exists to
stop a runaway, not to trim normal operation. At the fleet scale of C5
the load generator will want to lower it; the number is a constructor
argument for exactly that reason.
"""


@dataclass(frozen=True)
class QueuedEvent:
    """
    One TransactionEvent that could not be sent when it happened.

    Frozen: this is a record of something that already occurred. Nothing
    downstream has any business editing a reading after the fact, and a
    replay path that could rewrite history is a replay path that can
    quietly launder a bug into the results.

    The field names match agent/client.py's send_transaction_event
    parameters exactly, so replay is a keyword expansion rather than a
    hand-written mapping that can drift.
    """

    event_type: str
    transaction_id: str
    seq_no: int
    power_w: float
    energy_wh: float
    trigger_reason: str
    charging_state: str
    timestamp: str
    """ISO-8601, captured when the reading was TAKEN. See the module
    docstring -- this is load-bearing, not decoration."""

    token: str | None = None

    def describe(self) -> str:
        return (
            f"{self.event_type} seq={self.seq_no} "
            f"{self.power_w:.0f}W {self.energy_wh:.1f}Wh @{self.timestamp}"
        )


class OfflineQueue:
    """
    A bounded FIFO of events awaiting replay.

    STATION-scoped, like the state machine and the power backend. It
    must outlive connections -- that is the entire reason it exists --
    so it is created once in ChargingStation.__init__ and never
    recreated on reconnect.
    """

    def __init__(
        self,
        max_events: int = DEFAULT_MAX_EVENTS,
        station_id: str = "",
    ) -> None:
        if max_events < 1:
            raise ValueError(f"max_events must be >= 1, got {max_events}")

        self.max_events = max_events
        self.log = get_logger(__name__, station_id=station_id)

        # deque with maxlen drops from the left automatically when full.
        # Using that rather than checking-and-popping ourselves means
        # there is no window in which the queue is over its limit.
        self._events: deque[QueuedEvent] = deque(maxlen=max_events)

        self.queued_total = 0
        """Every event ever queued, including ones later dropped. With
        replayed_total and dropped_total this accounts for all three
        possible fates of a reading taken offline."""

        self.dropped_total = 0
        """Events lost to the cap. NON-ZERO MEANS THE DATASET HAS HOLES."""

        self.replayed_total = 0
        """Events successfully drained and handed to client.py."""

        self._warned_full = False
        """So the first drop shouts and the next four thousand do not.
        Reset when the queue empties, so a second outage warns again."""

    # -- filling it -------------------------------------------------------

    def append(self, event: QueuedEvent) -> bool:
        """
        Queue one event.

        Returns:
            True if it was queued cleanly, False if queuing it pushed an
            older event out. The caller does not have to act on False --
            the counters and the warning are handled here -- but the
            station's summary reads dropped_total, and a test asserts on
            the boundary.
        """
        self.queued_total += 1
        dropped = len(self._events) == self.max_events

        self._events.append(event)

        if dropped:
            self.dropped_total += 1
            if not self._warned_full:
                self._warned_full = True
                # ONE loud line the first time. See the module docstring:
                # a run with dropped events is still a usable run, but
                # only if this appears in the log.
                self.log.warning(
                    "offline queue is full at %d events -- the OLDEST readings "
                    "are now being discarded. This station's event log will "
                    "have a gap for the middle of this outage. Raise "
                    "max_events, or accept the gap as a documented "
                    "limitation.", self.max_events,
                )
            else:
                self.log.debug(
                    "offline queue full, dropped an older event (%d dropped so far)",
                    self.dropped_total,
                )

        return not dropped

    # -- emptying it --------------------------------------------------------

    def drain(self) -> list[QueuedEvent]:
        """
        Take everything, oldest first, and empty the queue.

        Returns a list rather than yielding, and empties before the
        caller sends anything. That ordering is deliberate: if a replay
        fails partway through because the connection died AGAIN -- which
        during E2 is likely, not hypothetical -- the events are already
        out of the queue and cannot be replayed twice. Track A detects
        duplicate seq_no values, and a double replay would look
        identical to a station with a broken counter.

        The cost is that events lost to a failed replay are gone. That
        is the right trade: a hole is honest, a duplicate is a lie.
        """
        if not self._events:
            return []

        events = list(self._events)
        self._events.clear()
        self._warned_full = False
        self.replayed_total += len(events)

        self.log.info(
            "draining %d queued event(s) for replay (seq %d..%d)",
            len(events), events[0].seq_no, events[-1].seq_no,
        )
        return events

    def clear(self) -> None:
        """
        Discard everything without replaying.

        Used when a transaction ends while offline: those events belong
        to a transaction the CSMS will never hear of, and replaying them
        after a new one has started would attach old readings to the
        wrong session.
        """
        if self._events:
            self.log.warning(
                "discarding %d queued event(s) without replay", len(self._events)
            )
        self._events.clear()
        self._warned_full = False

    # -- reading it ---------------------------------------------------------

    def __len__(self) -> int:
        return len(self._events)

    @property
    def is_empty(self) -> bool:
        return not self._events

    @property
    def is_full(self) -> bool:
        return len(self._events) == self.max_events

    def describe(self) -> str:
        """One line for the end-of-run summary."""
        text = (
            f"offline queue: {self.queued_total} queued, "
            f"{self.replayed_total} replayed, {self.dropped_total} dropped, "
            f"{len(self._events)} still pending"
        )
        if self.dropped_total:
            text += "  *** THIS STATION'S EVENT LOG HAS GAPS ***"
        return text