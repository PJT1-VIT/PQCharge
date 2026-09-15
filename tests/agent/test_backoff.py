"""
Tests for agent/backoff.py and agent/offline_queue.py.

Track C (tests). Phase C4.

No network, no event loop, no sleeping — these run in milliseconds.

--------------------------------------------------------------------
WHY THESE MATTER MORE THAN THEY LOOK

A backoff bug is invisible in an integration test. One station retrying
in lockstep with itself behaves identically to one station retrying
with perfect jitter — there is nothing to be out of step with. The
failure only appears at five hundred agents, where it produces a
recovery curve that looks like a real finding.

So the arithmetic is tested here, directly, with a seeded generator.
The jitter test in particular simulates a fleet: five hundred first
delays, and an assertion that they are actually spread out.
"""

from __future__ import annotations

import random
import statistics

import pytest

from agent.backoff import (
    JITTER_EQUAL,
    JITTER_FULL,
    JITTER_NONE,
    BackoffPolicy,
)
from agent.offline_queue import OfflineQueue, QueuedEvent


def _policy(**overrides) -> BackoffPolicy:
    defaults = dict(base_delay_s=1.0, max_delay_s=30.0, spread=1.0)
    defaults.update(overrides)
    return BackoffPolicy(**defaults)


def _event(seq: int = 0, power: float = 7400.0) -> QueuedEvent:
    return QueuedEvent(
        event_type="Updated",
        transaction_id="tx-1",
        seq_no=seq,
        power_w=power,
        energy_wh=float(seq),
        trigger_reason="MeterValuePeriodic",
        charging_state="Charging",
        timestamp=f"2026-09-14T10:00:{seq:02d}+00:00",
    )


# =====================================================================
# THE CURVE
# =====================================================================


def test_the_ceiling_doubles():
    p = _policy()
    assert [p.ceiling_for(n) for n in range(1, 6)] == [1.0, 2.0, 4.0, 8.0, 16.0]


def test_the_ceiling_stops_at_the_cap():
    """
    The cap is not cosmetic. E2 has a finite observation window, and an
    agent whose delay has grown to two minutes is effectively out of the
    run — its recovery would be recorded as "never" when the truth is
    "we stopped asking".
    """
    p = _policy(max_delay_s=10.0)
    assert p.ceiling_for(20) == 10.0
    assert p.ceiling_for(1000) == 10.0


def test_a_very_high_attempt_number_does_not_overflow():
    """
    2 ** 1100 is inf as a float, and inf seconds is a station that
    sleeps until the heat death of the universe. Reachable during a long
    unattended run against a server that never comes back.
    """
    assert _policy().ceiling_for(5000) == 30.0


def test_attempt_zero_is_rejected():
    """Attempts are 1-based. A caller passing 0 has an off-by-one, and
    silently treating it as 1 would hide it."""
    with pytest.raises(ValueError, match="attempt must be >= 1"):
        _policy().ceiling_for(0)


# =====================================================================
# JITTER — the one that matters
# =====================================================================


def test_full_jitter_spreads_a_fleet_across_the_whole_window():
    """
    THE test in this file.

    Five hundred agents, all failing at the same instant, all computing
    their first delay. If they cluster, E2 measures our thundering herd.

    The assertions are about SPREAD, not about any single value: the
    delays must cover most of the window, and no single 10% bucket may
    hold a large share of the fleet.
    """
    rng = random.Random(20260914)
    p = _policy(spread=1.0, strategy=JITTER_FULL)

    delays = [p.delay_for(1, rng) for _ in range(500)]

    assert min(delays) < 0.15, "nothing retried early — the window is not being used"
    assert max(delays) > 0.85, "nothing retried late — the window is not being used"

    # No 10% slice of the window holds more than a fifth of the fleet.
    # A lockstep implementation would put everything in one bucket.
    buckets = [0] * 10
    for d in delays:
        buckets[min(9, int(d * 10))] += 1
    assert max(buckets) < 100, f"delays are clustered: {buckets}"


def test_full_jitter_at_spread_one_is_classic_full_jitter():
    """uniform(0, ceiling) — the AWS strategy that measured best for
    client contention, which is exactly the E2 situation."""
    rng = random.Random(1)
    p = _policy(spread=1.0)
    delays = [p.delay_for(3, rng) for _ in range(400)]

    assert all(0.0 <= d <= 4.0 for d in delays)
    # Mean lands near the middle of the window rather than near the top.
    assert 1.6 < statistics.mean(delays) < 2.4


def test_a_smaller_spread_narrows_the_band_below_the_ceiling():
    """
    spread=0.3 — the old default — only randomises the top 30%. This
    test documents WHY that was changed: at the first retry it is a
    300 ms band, and five hundred agents inside 300 ms is still a herd.
    """
    rng = random.Random(7)
    p = _policy(spread=0.3)
    delays = [p.delay_for(1, rng) for _ in range(300)]

    assert all(0.7 <= d <= 1.0 for d in delays)
    assert max(delays) - min(delays) < 0.31


def test_no_jitter_is_deterministic():
    p = _policy(strategy=JITTER_NONE)
    assert p.delay_for(3) == p.delay_for(3) == 4.0


def test_zero_spread_is_deterministic_whatever_the_strategy():
    """A spread of 0 means no randomness, even under JITTER_FULL — so a
    run can be made exactly reproducible without changing strategy."""
    p = _policy(spread=0.0, strategy=JITTER_FULL)
    assert p.delay_for(2) == p.delay_for(2) == 2.0


def test_equal_jitter_never_retries_immediately():
    """
    Half fixed, half random. Its distinguishing property is a floor:
    unlike full jitter it never produces a near-zero delay, so a fleet
    never all arrives at once right after the failure.
    """
    rng = random.Random(3)
    p = _policy(strategy=JITTER_EQUAL)
    delays = [p.delay_for(2, rng) for _ in range(200)]

    assert all(1.0 <= d <= 2.0 for d in delays)
    assert min(delays) >= 1.0


def test_jitter_never_exceeds_the_ceiling():
    """A delay above the ceiling would break the cap, and with it the
    guarantee that an agent keeps trying inside the E2 window."""
    rng = random.Random(99)
    for strategy in (JITTER_FULL, JITTER_EQUAL, JITTER_NONE):
        p = _policy(strategy=strategy)
        for attempt in range(1, 8):
            ceiling = p.ceiling_for(attempt)
            for _ in range(50):
                assert 0.0 <= p.delay_for(attempt, rng) <= ceiling


# =====================================================================
# ATTEMPT LIMITS
# =====================================================================


def test_zero_max_attempts_means_forever():
    """What an E2 run wants. The server IS coming back; an agent that
    gave up would be counted as a failed recovery when it simply stopped
    trying."""
    p = _policy(max_attempts=0)
    assert p.should_retry(1) and p.should_retry(10_000)


def test_a_limit_is_honoured_exactly():
    p = _policy(max_attempts=3)
    assert [p.should_retry(n) for n in (1, 2, 3, 4)] == [True, True, True, False]


# =====================================================================
# VALIDATION — at construction, not at the first retry
# =====================================================================


@pytest.mark.parametrize(
    "kwargs, match",
    [
        (dict(base_delay_s=0), "base_delay_s"),
        (dict(base_delay_s=10.0, max_delay_s=1.0), "max_delay_s"),
        (dict(factor=0.5), "opposite of backoff"),
        (dict(spread=1.5), "spread"),
        (dict(strategy="wobble"), "unknown jitter strategy"),
        (dict(max_attempts=-1), "max_attempts"),
    ],
)
def test_bad_policies_are_rejected_at_construction(kwargs, match):
    """
    A bad backoff value discovered on the first disconnection is a bad
    value discovered forty minutes into a run, after the interesting
    part has already been recorded wrong.
    """
    with pytest.raises(ValueError, match=match):
        _policy(**kwargs)


def test_from_config_reads_an_agent_config():
    from agent.config import AgentConfig

    cfg = AgentConfig(
        station_id="CP001",
        reconnect_base_delay_s=2.0,
        reconnect_max_delay_s=60.0,
        reconnect_jitter=1.0,
        reconnect_max_attempts=5,
    )
    p = BackoffPolicy.from_config(cfg)

    assert (p.base_delay_s, p.max_delay_s, p.spread, p.max_attempts) == (
        2.0, 60.0, 1.0, 5,
    )


def test_the_shipped_default_jitter_is_high_enough_for_a_fleet():
    """
    Guards the decision made in C4. If someone lowers the default back
    to 0.3 for tidiness, this fails and says why.
    """
    from agent.config import AgentConfig

    assert AgentConfig(station_id="CP001").reconnect_jitter >= 1.0


def test_describe_names_the_jitter():
    """The effective jitter must appear in every run's log — it is the
    difference between a valid E2 measurement and an invalid one."""
    text = _policy(spread=1.0).describe()
    assert "jitter=full/1.0" in text


# =====================================================================
# THE OFFLINE QUEUE
# =====================================================================


def test_events_come_back_in_the_order_they_happened():
    """
    Replay order is not cosmetic. Track A logs a forward jump in seq_no
    as evidence of message loss during a storm — a real finding — and
    out-of-order replay would manufacture one.
    """
    q = OfflineQueue(max_events=10)
    for n in range(5):
        q.append(_event(seq=n))

    assert [e.seq_no for e in q.drain()] == [0, 1, 2, 3, 4]


def test_draining_empties_the_queue():
    """
    Empty-then-send, so a replay interrupted by a SECOND disconnection —
    likely during E2, not hypothetical — cannot send the same event
    twice. Track A detects duplicate seq_no values, and a double replay
    would look identical to a station with a broken counter.

    A hole is honest. A duplicate is a lie.
    """
    q = OfflineQueue()
    q.append(_event())
    q.drain()

    assert q.is_empty
    assert q.drain() == []


def test_the_queue_is_bounded_and_drops_the_oldest():
    """
    Five hundred agents queueing through a long outage is an
    out-of-memory kill of the load generator — which would end the run
    and look like a crash rather than a capacity limit.

    The readings nearest the reconnection are the ones kept.
    """
    q = OfflineQueue(max_events=3)
    for n in range(5):
        q.append(_event(seq=n))

    assert len(q) == 3
    assert [e.seq_no for e in q.drain()] == [2, 3, 4]


def test_drops_are_counted_so_the_gap_is_known():
    """
    A run with dropped events is still usable. A run with dropped events
    that nobody knew about is a dataset with an unexplained hole
    discovered at Stage 9.
    """
    q = OfflineQueue(max_events=2)
    for n in range(6):
        q.append(_event(seq=n))

    assert q.dropped_total == 4
    assert q.queued_total == 6
    assert "GAPS" in q.describe()


def test_append_reports_the_drop_boundary():
    q = OfflineQueue(max_events=2)
    assert q.append(_event(0)) is True
    assert q.append(_event(1)) is True
    assert q.append(_event(2)) is False  # this one pushed 0 out


def test_a_clean_run_does_not_claim_gaps():
    q = OfflineQueue(max_events=10)
    q.append(_event())
    q.drain()
    assert "GAPS" not in q.describe()
    assert q.dropped_total == 0


def test_clear_discards_without_counting_as_replayed():
    """
    Used when a new transaction starts. Those events belong to a
    transaction the CSMS will never hear of, and replaying them would
    attach old readings to a new transaction id — which Track A would
    accept without complaint and which would be unfindable at analysis.
    """
    q = OfflineQueue()
    q.append(_event())
    q.clear()

    assert q.is_empty
    assert q.replayed_total == 0


def test_the_original_timestamp_survives_the_queue():
    """
    Load-bearing, not decoration. csms/registry.py's last_meter_at guard
    compares incoming timestamps to decide whether a replayed reading
    may overwrite newer live state. Replaying with now() would make
    every stale event look newest, the guard would never fire, and Track
    A's defence against exactly this problem would be silently disabled
    by us.
    """
    q = OfflineQueue()
    q.append(_event(seq=3))
    assert q.drain()[0].timestamp == "2026-09-14T10:00:03+00:00"


def test_a_queued_event_cannot_be_edited_after_the_fact():
    """A replay path that can rewrite history is a replay path that can
    quietly launder a bug into the results."""
    with pytest.raises(Exception):
        _event().power_w = 0.0  # type: ignore[misc]


def test_a_zero_capacity_queue_is_rejected():
    with pytest.raises(ValueError, match="max_events"):
        OfflineQueue(max_events=0)