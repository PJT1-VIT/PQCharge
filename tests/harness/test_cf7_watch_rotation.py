"""
C-F7: the console view (harness/watch_migration.py) during a KEY ROTATION.
Track C (tests; tracker step 8, T-1).

Driven by the dashboard's mock fleet, whose /api/fleet and /api/migration
shapes match the real server's (A-F4 fields pq_key_id and last_pq_check;
MigrationStatus with kind "rotation"), so the view reads exactly what it
reads live.
"""

from __future__ import annotations

from datetime import datetime, timezone

import harness.watch_migration as wm
from dashboard.mock import MockFleet, STEP_S


class Clock:
    def __init__(self) -> None:
        self.t = 1_790_000_000.0

    def __call__(self) -> float:
        return self.t


def _poll(fleet: MockFleet) -> wm.Poll:
    return wm.Poll(fleet=fleet.fleet(), migration=fleet.migration())


def _run(fleet: MockFleet, clock: Clock, tracker: wm.Tracker, limit_s: float = 60.0) -> None:
    waited = 0.0
    while waited < limit_s:
        clock.t += STEP_S
        waited += STEP_S
        tracker.update(_poll(fleet), now=waited)
        if tracker.finished:
            return
    raise AssertionError("did not finish")


def _grid(text: str) -> str:
    return "".join(line.split(None, 1)[1].replace(" ", "")
                   for line in text.splitlines() if line.startswith("  CP"))


def _migrated_fleet(n: int = 12) -> tuple[MockFleet, Clock]:
    clock = Clock()
    fleet = MockFleet(n=n, clock=clock)
    fleet.start(wave_size=5, canary_count=3)
    tracker = wm.Tracker(started=0)
    tracker.update(_poll(fleet), now=0)
    _run(fleet, clock, tracker)
    return fleet, clock


def test_a_rotation_is_shown_with_its_own_words_letters_and_key_changes():
    fleet, clock = _migrated_fleet()
    fleet.refusers = {"CP0010"}                     # one charger cannot prove its new key
    tracker = wm.Tracker(started=0)
    tracker.update(_poll(fleet), now=0)             # before the rotation: keys_at_start
    fleet.rotate(wave_size=10, canary_count=3)
    _run(fleet, clock, tracker)

    poll = _poll(fleet)
    text = wm.render(poll, tracker, now_utc=datetime.fromtimestamp(clock.t, tz=timezone.utc))
    assert "key rotation" in text and "new key 12" in text and "kept old key 1" in text
    assert "N new key" in text and "! failed, old key kept" in text
    grid = _grid(text)
    assert grid.count("N") == 12 and grid.count("!") == 1          # CP0000 + CP0001-12, CP0010 failed
    changes = [(sid, old, new, ) for _t, sid, old, new in tracker.changes]
    assert changes and all(old.startswith("key ") and new.startswith("key ") for _s, old, new in changes)
    assert "(new key, no disconnect)" in text
    summary = wm.summary(poll)
    assert summary.startswith("Key rotation") and "12 new keys, 1 kept their old key" in summary


def test_a_rolled_back_rotation_wave_says_the_old_key_was_put_back():
    fleet, clock = _migrated_fleet(n=5)
    fleet.refusers = {"CP0005"}                     # 1 of 3 in the last wave fails: 33 % > 20 %
    tracker = wm.Tracker(started=0)
    tracker.update(_poll(fleet), now=0)
    fleet.rotate(wave_size=3, canary_count=3)
    _run(fleet, clock, tracker)

    text = wm.render(_poll(fleet), tracker, now_utc=datetime.fromtimestamp(clock.t, tz=timezone.utc))
    assert "ROLLED_BACK" in text and "KEEP THEIR OLD KEYS" in text
    assert "(old key put back)" in text


def test_a_migration_is_still_shown_as_a_migration():
    clock = Clock()
    fleet = MockFleet(n=6, clock=clock)
    tracker = wm.Tracker(started=0)
    tracker.update(_poll(fleet), now=0)
    fleet.start(wave_size=3, canary_count=2)
    _run(fleet, clock, tracker)
    text = wm.render(_poll(fleet), tracker)
    assert "fleet migration" in text and "U upgraded" in text and "key rotation" not in text
    assert _grid(text) == "U" * 7
