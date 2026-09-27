"""
E5 — SECURITY: were impostors kept out, and did commands behave?

Track C (analysis). Phase C6.

--------------------------------------------------------------------
IN PLAIN WORDS

E5's demonstration: an attacker pretending to be the operator (or a
charger) sends commands. On a classical fleet with a stolen identity the
commands work -- every charger jumps to full power and the fleet power
line spikes. On a migrated fleet the fake identity fails the post-quantum
check and nothing happens.

This module reports the SECURITY FACTS of a run:
    - certificate identity checks at connect time: passed / rejected
      (a charger whose certificate names a different charger is rejected)
    - connection failures, by reason
    - protocol errors (CALLErrors) and commands the chargers received

The POWER picture that goes with it is the fleet power timeline in the
run overview -- the same timeline, so the spike (or its absence) is read
off one chart.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from analysis.match import MatchedRun


def measure(run: MatchedRun) -> dict[str, Any]:
    checks = [
        e for e in run.events("connection_attempt")
        if (e.get("payload") or {}).get("transition") == "identity_check"
    ]
    rejected = [e for e in checks if e.get("outcome") == "rejected"]

    failures: Counter[str] = Counter(
        str((e.get("payload") or {}).get("reason") or "unknown")
        for e in run.events("connection_failed")
    )

    rows = run.harness.of_type("station_finished", "station_crashed")
    return {
        "identity_checks": len(checks),
        "identity_rejected": len(rejected),
        "identity_rejections": [
            {
                "station_id": e.get("station_id"),
                "certificate_name": (e.get("payload") or {}).get("certificate_common_name"),
                "at_s": round(e["_t"] - (run.harness.started_at or e["_t"]), 3),
            }
            for e in rejected[:50]
        ],
        "connection_failures": dict(failures),
        "callerrors": float(sum((r.get("callerrors") or 0) for r in rows)),
        "commands_received": float(sum((r.get("commands_received") or 0) for r in rows)),
    }
