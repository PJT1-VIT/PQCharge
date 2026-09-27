"""
STEP 3 of the analysis — MATCH: pair each tester run with the server's view of it.

Track C (analysis). Phase C6.

--------------------------------------------------------------------
IN PLAIN WORDS

The tester and the server each give a run its OWN id -- they are separate
programs, and the server may even restart in the middle of a run (E2 does
that on purpose, and every restart is a new server id). So the two diaries
cannot be joined by id. They are joined by:

    WHO   the charger ids the tester started (CP0001 ... CP0500), and
    WHEN  the time window between the tester's "run started" and
          "run finished" lines, widened by a small margin.

Every server line about one of those chargers, inside that window, belongs
to this run. Server-wide lines (server started / stopping, migration waves)
inside the window are kept too -- they are the E2 and E3 anchor points.

The margin absorbs small scheduling delays. Both diaries stamp wall-clock
UTC time; if the server ran on a DIFFERENT machine whose clock is off by
more than the margin, the match comes back empty and the trust check says so
rather than guessing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from analysis.collect import HarnessRun, ServerDiary

WINDOW_BEFORE_S = 2.0
WINDOW_AFTER_S = 5.0


@dataclass
class MatchedRun:
    """One tester run and every server line that belongs to it."""

    harness: HarnessRun
    station_events: list[dict[str, Any]] = field(default_factory=list)
    """Server lines about this run's chargers, in time order."""

    server_events: list[dict[str, Any]] = field(default_factory=list)
    """Server-wide lines (no charger id) inside the window, in time order."""

    window: tuple[float, float] | None = None

    @property
    def server_run_ids(self) -> list[str]:
        seen: dict[str, None] = {}
        for ev in self.server_events + self.station_events:
            if ev.get("run_id"):
                seen[ev["run_id"]] = None
        return list(seen)

    @property
    def stations(self) -> list[str]:
        return self.harness.station_ids

    def events(self, *event_types: str, station_id: str | None = None) -> list[dict[str, Any]]:
        """Lines of the given types. Server-wide types come from the
        server-wide list, per-charger types from the charger list."""
        wanted = set(event_types)
        pool = self.server_events if wanted <= SERVER_WIDE else self.station_events
        out = [e for e in pool if e.get("event_type") in wanted]
        if station_id is not None:
            out = [e for e in out if e.get("station_id") == station_id]
        return out

    def transitions(self, name: str) -> list[dict[str, Any]]:
        """Server lines whose payload.transition equals `name` (e.g. "booted")."""
        return [
            e for e in self.station_events
            if (e.get("payload") or {}).get("transition") == name
        ]


# Server lines that are about the server itself, not about one charger.
SERVER_WIDE = frozenset({
    "server_started", "server_stopping",
    "migration_started", "wave_started", "wave_completed",
    "wave_rolled_back", "migration_completed",
})


def match_run(run: HarnessRun, diary: ServerDiary) -> MatchedRun:
    matched = MatchedRun(harness=run)
    start = run.started_at
    end = run.finished_at or run.last_seen_at
    if start is None or end is None:
        return matched

    lo, hi = start - WINDOW_BEFORE_S, end + WINDOW_AFTER_S
    matched.window = (lo, hi)
    ours = set(run.station_ids)

    for ev in diary.events:
        t = ev.get("_t")
        if t is None or t < lo or t > hi:
            continue
        sid = ev.get("station_id")
        if sid is None:
            matched.server_events.append(ev)
        elif sid in ours:
            matched.station_events.append(ev)

    matched.station_events.sort(key=lambda e: e["_t"])
    matched.server_events.sort(key=lambda e: e["_t"])
    return matched
