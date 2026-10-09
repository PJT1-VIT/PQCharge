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
      (a charger whose certificate names a different charger is rejected),
      plus mismatches let in by Track A's default "warn" mode
    - post-quantum key checks (C6.1): passed / rejected, with the reason
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
    # In Track A's default "warn" mode a wrong certificate is LET IN and
    # logged with identity_matches false (outcome success). Counted on its
    # own, so a warn-mode run still shows the impostor it would have refused.
    mismatched = [
        e for e in checks if (e.get("payload") or {}).get("identity_matches") is False
    ]

    # PHASE C6.1: the post-quantum key checks (pq_auth). Fleet-wide, because
    # a failing charger may be one the tester did not start. Result is in
    # `outcome` (filled from payload.result by collect.py).
    pq = run.pq_checks()
    pq_rejected = [e for e in pq if (e.get("outcome") or "") != "success"]

    # C-P4 (Contract 7 sections 7.5, 7.6): split by why the check ran, and
    # count the chargers cut off for failing the check after boot (E5's
    # attack demonstration: a charger with a wrong key is refused).
    def split(trigger: str) -> dict[str, int]:
        rows = run.pq_checks(trigger)
        ok = sum(1 for e in rows if (e.get("outcome") or "") == "success")
        return {"checks": len(rows), "passed": ok, "rejected": len(rows) - ok}

    cut_off = [
        e for e in run.events("connection_closed")
        if (e.get("payload") or {}).get("reason") == "pq_auth_failed"
    ]

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
        "identity_mismatches": len(mismatched),
        "identity_mode": next(
            ((e.get("payload") or {}).get("identity_check") for e in checks
             if (e.get("payload") or {}).get("identity_check")), None),
        "pq_checks": len(pq),
        "pq_passed": len(pq) - len(pq_rejected),
        "pq_rejected": len(pq_rejected),
        "pq_rejections": [
            {
                "station_id": e.get("station_id"),
                "detail": (e.get("payload") or {}).get("detail"),
                "trigger": (e.get("payload") or {}).get("trigger") or "migration",
                "at_s": round(e["_t"] - (run.harness.started_at or e["_t"]), 3),
            }
            for e in pq_rejected[:50]
        ],
        "pq_by_trigger": {"migration": split("migration"), "boot": split("boot")},
        "pq_cut_off": len(cut_off),
        "pq_cut_off_ids": sorted({e.get("station_id") for e in cut_off if e.get("station_id")})[:50],
        "connection_failures": dict(failures),
        "callerrors": float(sum((r.get("callerrors") or 0) for r in rows)),
        "commands_received": float(sum((r.get("commands_received") or 0) for r in rows)),
    }
