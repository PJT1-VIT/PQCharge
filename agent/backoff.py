"""
Retry timing — how long to wait before trying the CSMS again.

Track C (agent). Phase C4.

--------------------------------------------------------------------
WHERE THIS FITS

    agent/config.py        the numbers, from the command line
          |
    agent/backoff.py    <- you are here. Pure arithmetic. No asyncio,
          |                no clock, no sleeping. Answers one question:
          |                "how long should attempt N wait?"
    agent/station.py       asks, then sleeps

Pure and clock-free on purpose. A backoff bug is invisible in a
single-agent test -- one station retrying in lockstep with itself looks
identical to one station retrying with perfect jitter -- and only shows
up at five hundred agents, where it is indistinguishable from a real
finding. So the arithmetic is separated out and tested directly, with
an injectable random source so the tests are deterministic.

--------------------------------------------------------------------
*** WHY JITTER IS THE HIGHEST-RISK LINE OF CODE IN TRACK C ***

E2 kills the CSMS and times how long the fleet takes to recover. That
number is the project's headline contribution.

Without jitter, all five hundred agents fail at the same moment, wait
the same delay, and retry on the same tick. The server -- which has
just restarted and is doing TLS handshakes -- gets five hundred
simultaneous connections, refuses or stalls most of them, and every
refused agent then waits the same doubled delay and retries together
again. The recovery curve that comes out is a staircase produced by our
own retry loop.

It would look completely plausible. It would get written up as the
operational cost of post-quantum cryptography. It is not: it is a
thundering herd of our own making, and post-quantum handshakes being
slower would make the staircase worse, which would make the wrong
conclusion look better supported.

Jitter is what makes the measurement measure the thing.

--------------------------------------------------------------------
THE THREE STRATEGIES, AND WHY "FULL" IS THE DEFAULT

Named after the strategies in AWS's "Exponential Backoff And Jitter",
which measured them against each other rather than reasoning about
them:

    NONE    delay = ceiling
            Deterministic. Only for tests that need a predictable
            delay, and for showing in a report what the unjittered
            curve looks like. NEVER for an E2 run.

    EQUAL   delay = ceiling/2 + uniform(0, ceiling/2)
            Half fixed, half random. Retries stay reasonably prompt but
            spread over half the window. A middle option.

    FULL    delay = uniform(ceiling * (1 - spread), ceiling)
            The default. With spread = 1.0 this is uniform(0, ceiling)
            -- classic full jitter, the strategy that measured best for
            total work done and client contention, which is exactly the
            E2 situation.

The `spread` knob is agent/config.py's reconnect_jitter. It exists so a
run can be made deliberately less jittered to DEMONSTRATE the
thundering herd, which is a legitimate thing to want to show in a
report -- and so that the value used is recorded in the log rather than
being a constant nobody can see afterwards.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

JITTER_NONE = "none"
JITTER_EQUAL = "equal"
JITTER_FULL = "full"

JITTER_STRATEGIES = (JITTER_NONE, JITTER_EQUAL, JITTER_FULL)


@dataclass(frozen=True)
class BackoffPolicy:
    """
    How long attempt N waits, and whether there should be an attempt N.

    Frozen: a policy is a decision made once at start-up and recorded in
    the log. Something that mutated its own delays mid-run would make a
    run unreproducible, and every E2 figure is a comparison across runs.
    """

    base_delay_s: float = 1.0
    """Ceiling for the first retry. Doubles from here."""

    max_delay_s: float = 30.0
    """
    The ceiling stops growing here.

    Matters more than it looks during E2. The experiment has a finite
    observation window; an agent whose delay has grown to two minutes is
    effectively out of the run, and its recovery time would be recorded
    as "never" when the truth is "we stopped asking".
    """

    factor: float = 2.0
    """Growth per attempt. 2.0 is exponential backoff."""

    spread: float = 1.0
    """
    How much of the ceiling is randomised, 0.0 to 1.0.

    1.0 means the delay is uniform across the whole window -- maximum
    spreading, the right default for a fleet. 0.0 means no jitter at
    all. Comes from config.reconnect_jitter.
    """

    strategy: str = JITTER_FULL
    max_attempts: int = 0
    """0 means retry forever, which is what an E2 run wants: the server
    is coming back, and an agent that gave up would be counted as a
    failed recovery when it simply stopped trying."""

    def __post_init__(self) -> None:
        """
        Validate at construction, not at the first retry.

        A bad backoff value discovered on the first disconnection is a
        bad value discovered forty minutes into a run, after the
        interesting part has already been recorded wrong.
        """
        if self.base_delay_s <= 0:
            raise ValueError(f"base_delay_s must be > 0, got {self.base_delay_s}")
        if self.max_delay_s < self.base_delay_s:
            raise ValueError(
                f"max_delay_s ({self.max_delay_s}) must be >= base_delay_s "
                f"({self.base_delay_s})"
            )
        if self.factor < 1.0:
            raise ValueError(
                f"factor must be >= 1.0, got {self.factor} -- a factor below 1 "
                f"makes retries get FASTER after each failure, which is the "
                f"opposite of backoff"
            )
        if not 0.0 <= self.spread <= 1.0:
            raise ValueError(f"spread must be 0.0-1.0, got {self.spread}")
        if self.strategy not in JITTER_STRATEGIES:
            raise ValueError(
                f"unknown jitter strategy {self.strategy!r}; "
                f"expected one of {JITTER_STRATEGIES}"
            )
        if self.max_attempts < 0:
            raise ValueError(
                f"max_attempts must be >= 0 (0 means forever), "
                f"got {self.max_attempts}"
            )

    # -- construction from configuration -----------------------------------

    @classmethod
    def from_config(cls, config: object) -> BackoffPolicy:
        """
        Build from an AgentConfig.

        Takes `object` rather than AgentConfig so this module imports
        nothing of ours -- the same rule config.py and logging_setup.py
        follow, and what keeps the dependency graph acyclic.
        """
        return cls(
            base_delay_s=getattr(config, "reconnect_base_delay_s", 1.0),
            max_delay_s=getattr(config, "reconnect_max_delay_s", 30.0),
            spread=getattr(config, "reconnect_jitter", 1.0),
            max_attempts=getattr(config, "reconnect_max_attempts", 0),
        )

    # -- the arithmetic --------------------------------------------------------

    def ceiling_for(self, attempt: int) -> float:
        """
        The un-jittered delay for this attempt. Attempts start at 1.

        Exposed separately because it is what a report plots as "the
        backoff curve", and because every jitter strategy below is
        defined relative to it.
        """
        if attempt < 1:
            raise ValueError(f"attempt must be >= 1, got {attempt}")

        # Computed rather than accumulated, so a policy has no state and
        # a station that reconnects does not carry a stale multiplier.
        # Capped before the exponent can overflow: at factor 2 and
        # attempt 1100 the float would become inf, and inf * anything is
        # a station that sleeps forever.
        if attempt > 64:
            return self.max_delay_s

        return min(self.max_delay_s, self.base_delay_s * (self.factor ** (attempt - 1)))

    def delay_for(
        self, attempt: int, rng: random.Random | None = None
    ) -> float:
        """
        How long attempt N should actually wait.

        Args:
            attempt: 1 for the first retry.
            rng: injectable for tests. Production passes None and gets
                the module-global random, which is seeded per process --
                so five hundred agents in ONE process share a generator
                and are properly decorrelated from each other. (They
                would also be decorrelated with per-agent generators,
                but only if each were seeded differently, and "every
                agent seeded from the same default" is exactly the kind
                of mistake that produces synchronised retries while
                looking like it has jitter.)
        """
        ceiling = self.ceiling_for(attempt)
        source = rng if rng is not None else random

        if self.strategy == JITTER_NONE or self.spread <= 0.0:
            return ceiling

        if self.strategy == JITTER_EQUAL:
            half = ceiling / 2.0
            return half + source.uniform(0.0, half)

        # JITTER_FULL. At spread=1.0 the lower bound is 0, which is
        # classic full jitter.
        return source.uniform(ceiling * (1.0 - self.spread), ceiling)

    def should_retry(self, attempt: int) -> bool:
        """
        Whether attempt N is allowed to happen at all.

        `attempt` is the attempt about to be made. With max_attempts 0
        this is always True.
        """
        return self.max_attempts == 0 or attempt <= self.max_attempts

    # -- diagnostics -------------------------------------------------------------

    def describe(self) -> str:
        """
        One line for the start-up log.

        The effective jitter MUST appear in the log of every run. It is
        the difference between a valid E2 measurement and an invalid
        one, and six weeks later the only record of which was used is
        this line.
        """
        attempts = "forever" if self.max_attempts == 0 else f"{self.max_attempts} max"
        return (
            f"backoff base={self.base_delay_s}s cap={self.max_delay_s}s "
            f"factor={self.factor} jitter={self.strategy}/{self.spread} "
            f"attempts={attempts}"
        )

    def preview(self, attempts: int = 6) -> str:
        """
        The first few ceilings, for a start-up log line.

        Un-jittered, because the point is to show the shape. Cheap way
        to notice at a glance that a run is configured to give up after
        four seconds or to sleep for five minutes.
        """
        return " ".join(f"{self.ceiling_for(n):g}s" for n in range(1, attempts + 1))