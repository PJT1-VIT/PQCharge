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

C-F5 (after Track A's A-F7, experiments/e5_attack.md + e5_check.py):
    - identity refusals, 1008 cut-offs and curtailment commands are counted
      for EVERY charger in the run's window, not only the tester's: the
      victim (CP0003 small run, CP0045 demo) runs in its own window, outside
      the load generator. `outside_ids` names those chargers.
    - `verdicts` gives the same three answers as
      `python -m experiments.e5_check`, by the same rules:
        stolen_certificate  "stopped"   boot checks rejected > 0 and
                                        1008 cut-offs == boot rejections
                            "check"     both seen, numbers differ
                            "not_seen"  no rejected boot check
        wrong_identity      "stopped"   any identity_check refused, else "not_seen"
        curtailment         "done"      any Set/ClearChargingProfile sent, else "not_seen"
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from analysis.match import MatchedRun


CURTAILMENT_ACTIONS = ("SetChargingProfile", "ClearChargingProfile")


def measure(run: MatchedRun) -> dict[str, Any]:
    ours = set(run.stations)
    checks = [
        e for e in run.fleet_events("connection_attempt")
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
        e for e in run.fleet_events("connection_closed")
        if (e.get("payload") or {}).get("reason") == "pq_auth_failed"
    ]

    # (c) curtailment: the server's own record of the commands it sent
    # (dispatcher message_sent lines), as e5_check counts them.
    curtail: Counter[tuple[str, str]] = Counter(
        (str((e.get("payload") or {}).get("action")), str(e.get("outcome") or "unknown"))
        for e in run.fleet_events("message_sent")
        if (e.get("payload") or {}).get("dispatched")
        and (e.get("payload") or {}).get("action") in CURTAILMENT_ACTIONS
    )
    curtailment = {
        action: {outcome: n for (a, outcome), n in sorted(curtail.items()) if a == action}
        for action in CURTAILMENT_ACTIONS
    }

    boot = split("boot")
    if not boot["rejected"]:
        stolen = "not_seen"
    elif cut_off and len(cut_off) == boot["rejected"]:
        stolen = "stopped"
    else:
        stolen = "check"
    outside = sorted({
        sid for sid in (
            [e.get("station_id") for e in checks + cut_off]
            + [(e.get("payload") or {}).get("station") for e in pq]
        ) if sid and sid not in ours
    })

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
        "pq_by_trigger": {"migration": split("migration"), "boot": boot,
                          "rotation": split("rotation")},
        "pq_cut_off": len(cut_off),
        "pq_cut_off_ids": sorted({e.get("station_id") for e in cut_off if e.get("station_id")})[:50],
        "connection_failures": dict(failures),
        "callerrors": float(sum((r.get("callerrors") or 0) for r in rows)),
        "commands_received": float(sum((r.get("commands_received") or 0) for r in rows)),
        "curtailment": curtailment,
        "outside_ids": outside[:50],
        "verdicts": {
            "stolen_certificate": stolen,
            "wrong_identity": "stopped" if rejected else "not_seen",
            "curtailment": "done" if curtail else "not_seen",
        },
    }
