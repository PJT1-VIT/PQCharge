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

    migration_events: list[dict[str, Any]] = field(default_factory=list)
    """
    PHASE C6.1. Every migration line inside the window, for the WHOLE
    fleet -- not only this run's chargers. A migration is fleet-wide: in
    Stage 6 the five chargers that fail are Track A's test chargers, which
    the tester did not start. Contains the orchestrator's lines (source
    "orchestrator": migration/wave events, pq_auth checks, deferrals,
    migration_failed) and the server's own manual-rollback line.
    """

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

    def migration(self, *event_types: str, transition: str | None = None) -> list[dict[str, Any]]:
        """Fleet-wide migration lines of the given types (all if none given)."""
        wanted = set(event_types)
        out = [e for e in self.migration_events if not wanted or e.get("event_type") in wanted]
        if transition is not None:
            out = [e for e in out if (e.get("payload") or {}).get("transition") == transition]
        return out

    @property
    def server_mode(self) -> str | None:
        """
        PHASE C-P4. The crypto_mode the SERVER wrote on this run's lines
        (Contract 3 puts it on every line); the most common one if mixed.
        None when the server has no lines for this run.
        """
        counts: dict[str, int] = {}
        for ev in self.station_events + self.server_events:
            mode = ev.get("crypto_mode")
            if mode:
                counts[mode] = counts.get(mode, 0) + 1
        return max(counts, key=lambda m: counts[m]) if counts else None

    def pq_checks(self, trigger: str | None = None) -> list[dict[str, Any]]:
        """
        PHASE C-P4. Fleet-wide key checks (connection_attempt, transition
        "pq_auth"). trigger="boot" -> the checks after every accepted boot
        (Contract 7 section 7.5); trigger="migration" -> the orchestrator's.
        A line with no trigger (written before Contract 7) is a migration
        check: that was the only kind.
        """
        rows = self.migration("connection_attempt", transition="pq_auth")
        if trigger is None:
            return rows
        return [e for e in rows
                if ((e.get("payload") or {}).get("trigger") or "migration") == trigger]

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
    "wave_rolled_back", "migration_completed", "migration_failed",
})

# PHASE C6.1: lines that belong to a migration, whoever the charger is.
MIGRATION_TYPES = frozenset({
    "migration_started", "wave_started", "wave_completed", "wave_rolled_back",
    "migration_completed", "migration_failed", "station_deferred",
})


def is_migration_line(ev: dict[str, Any]) -> bool:
    p = ev.get("payload") or {}
    return (
        ev.get("event_type") in MIGRATION_TYPES
        or p.get("source") == "orchestrator"
        or (ev.get("event_type") == "connection_attempt" and p.get("transition") == "pq_auth")
    )


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
        if is_migration_line(ev):
            matched.migration_events.append(ev)
        sid = ev.get("station_id")
        if sid is None:
            matched.server_events.append(ev)
        elif sid in ours:
            matched.station_events.append(ev)

    matched.station_events.sort(key=lambda e: e["_t"])
    matched.server_events.sort(key=lambda e: e["_t"])
    matched.migration_events.sort(key=lambda e: e["_t"])
    return matched
