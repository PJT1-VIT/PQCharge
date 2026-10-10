"""
E5 check -- did each attack get stopped? Reads one server event log.

Track A (A-F7). Usage:

    python -m experiments.e5_check logs/e5_events.jsonl [--station CP0003]

Prints one verdict per attack in plain words, from the server's own lines
(Contract 3), so the panel sees the same evidence Track C's E5 analysis
(analysis/measures/e5_security.py) counts:

  (a) stolen certificate, no ML-DSA key  -- boot key checks with
      trigger "boot" + result "rejected", and connection_closed with
      payload.reason "pq_auth_failed" (closed with 1008), per station.
  (b) wrong identity (another station's certificate) -- identity_check
      lines with outcome "rejected" (refused by --tls-identity-check enforce).
  (c) curtailment -- SetChargingProfile / ClearChargingProfile commands
      sent and accepted (message_sent lines written by the dispatcher).

Exit code 0 always: this is a report, not a test.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def load(path: Path) -> list[dict]:
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue   # a run killed mid-write can leave one partial line
    return events


def summarise(events: list[dict], station: str | None = None) -> dict:
    def mine(sid):
        return station is None or sid == station

    boot_checks = [
        e["payload"] for e in events
        if e.get("event_type") == "connection_attempt"
        and (e.get("payload") or {}).get("transition") == "pq_auth"
        and (e.get("payload") or {}).get("trigger") == "boot"
        and mine((e.get("payload") or {}).get("station"))
    ]
    cut_off = [
        e for e in events
        if e.get("event_type") == "connection_closed"
        and (e.get("payload") or {}).get("reason") == "pq_auth_failed"
        and mine(e.get("station_id"))
    ]
    identity = [
        e for e in events
        if e.get("event_type") == "connection_attempt"
        and (e.get("payload") or {}).get("transition") == "identity_check"
        and mine(e.get("station_id"))
    ]
    commands = [
        e for e in events
        if e.get("event_type") == "message_sent"
        and (e.get("payload") or {}).get("dispatched")
        and (e.get("payload") or {}).get("action") in ("SetChargingProfile", "ClearChargingProfile")
    ]
    return {
        "boot_checks": Counter((p.get("station"), p.get("result")) for p in boot_checks),
        "cut_off": Counter(e.get("station_id") for e in cut_off),
        "identity_rejected": [
            (e.get("station_id"), (e.get("payload") or {}).get("certificate_common_name"))
            for e in identity if e.get("outcome") == "rejected"
        ],
        "identity_accepted": sum(1 for e in identity if e.get("outcome") == "success"),
        "commands": Counter(
            ((e.get("payload") or {}).get("action"), e.get("outcome")) for e in commands
        ),
    }


def report(s: dict) -> str:
    lines = []
    rejected = sum(n for (_sid, result), n in s["boot_checks"].items() if result == "rejected")
    passed = sum(n for (_sid, result), n in s["boot_checks"].items() if result == "success")
    cut = sum(s["cut_off"].values())
    lines.append("(a) stolen certificate, no ML-DSA key")
    lines.append(f"    boot key checks: {rejected} rejected, {passed} passed")
    lines.append(f"    connections closed 1008 (pq_auth_failed): {cut} "
                 f"{dict(s['cut_off']) if cut else ''}".rstrip())
    lines.append("    -> STOPPED" if cut and cut == rejected else
                 "    -> not seen in this log" if not rejected else
                 "    -> CHECK: rejected checks and closes differ")
    lines.append("(b) wrong identity (another station's certificate)")
    if s["identity_rejected"]:
        for sid, cn in s["identity_rejected"]:
            lines.append(f"    refused: connected as {sid} with a certificate for {cn}")
        lines.append("    -> STOPPED")
    else:
        lines.append("    -> not seen in this log (TLS off, or no such attempt)")
    lines.append("(c) curtailment (cyber-physical actuation)")
    if s["commands"]:
        for (action, outcome), n in sorted(s["commands"].items()):
            lines.append(f"    {action}: {n} x {outcome}")
        lines.append("    -> DONE")
    else:
        lines.append("    -> not seen in this log")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="E5 attack verdicts from a server event log")
    parser.add_argument("log", type=Path)
    parser.add_argument("--station", help="only this station (e.g. the victim, CP0003)")
    args = parser.parse_args()
    print(report(summarise(load(args.log), args.station)))


if __name__ == "__main__":
    main()
