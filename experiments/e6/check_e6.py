"""
E6 checker -- did a third-party OCPP client complete a charging session on
our CSMS, and what did it send that we could not handle?

Track A (csms). Day 10.

Reads only what the CSMS itself wrote -- nothing from the simulator -- so the
verdict is the server's own evidence:

  --events      the Contract 3 event log (python -m csms.server --log ...)
  --server-log  the server's console output, saved with `| tee`. Optional but
                recommended: the ocpp library logs every raw inbound message
                there ("receive message [2, ...]") and every request it could
                not handle ("Error while handling request"), which is how an
                unimplemented action or a schema violation shows up.

Stations whose id looks like our own fleet (CP + digits) are ignored, so the
report is about the external client only.

Usage:
    python experiments/e6/check_e6.py --events logs/e6_events.jsonl \
        --server-log logs/e6_server.log
Exit code 0 = E6 PASS, 1 = FAIL. The table is printed either way.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

OUR_FLEET = re.compile(r"^CP\d+$")
"""Our own stations (CP0001, CP001 ...). Everything else the server saw is
treated as external -- the same rule Track C's analysis/measures/e6_nodes.py
uses ("chargers the tester did not start")."""

RECEIVE = re.compile(r"(\S+): receive message (\[.*)$")
UNHANDLED = re.compile(r"Error while handling request '(.*)'")
OCPP_ERROR = re.compile(r"^(ocpp\.exceptions\.\w+): ?(.*)$")
CALL_ACTION = re.compile(r"action=['\"]?(\w+)")


def read_text(path: Path) -> str:
    """Read a log whatever the shell wrote it as. Windows PowerShell 5's
    Tee-Object writes UTF-16; Git Bash and macOS write UTF-8."""
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def scan_events(path: Path) -> dict[str, dict]:
    per: dict[str, dict] = defaultdict(lambda: {
        "connections": 0, "booted": 0, "authorize": Counter(),
        "tx_started": 0, "tx_updated": 0, "tx_ended": 0, "energy_wh": 0.0,
        "unrecognised": set(), "vendor_model": None,
    })
    for line in read_text(path).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue  # a truncated last line after a kill is expected
        sid = e.get("station_id")
        if not sid or OUR_FLEET.match(sid):
            continue
        p = e.get("payload") or {}
        et = e.get("event_type")
        rec = per[sid]
        if et == "connection_established":
            rec["connections"] += 1
        elif et == "state_changed" and p.get("transition") == "booted":
            rec["booted"] += 1
            rec["vendor_model"] = f"{p.get('vendor_name')} / {p.get('model')}"
        elif et == "state_changed" and p.get("transition") == "authorize":
            rec["authorize"][str(p.get("status"))] += 1
        elif et == "transaction_started":
            rec["tx_started"] += 1
        elif et == "transaction_ended":
            rec["tx_ended"] += 1
            rec["energy_wh"] += float(p.get("energy_wh") or 0.0)
        elif et == "transaction_updated":
            if p.get("transition") == "unrecognised_meter_value":
                rec["unrecognised"].update(p.get("measurands") or [])
            else:
                rec["tx_updated"] += 1
    return per


def scan_server_log(path: Path) -> tuple[dict[str, Counter], list[tuple[str, str, str]]]:
    """(actions received per station, [(action, error class, message)])."""
    received: dict[str, Counter] = defaultdict(Counter)
    unhandled: list[tuple[str, str, str]] = []
    pending_action: str | None = None
    for line in read_text(path).splitlines():
        m = RECEIVE.search(line)
        if m:
            sid, raw = m.group(1), m.group(2)
            if not OUR_FLEET.match(sid):
                try:
                    msg = json.loads(raw)
                    if msg and msg[0] == 2 and len(msg) > 2:
                        received[sid][msg[2]] += 1
                except (json.JSONDecodeError, TypeError, IndexError):
                    pass
            continue
        m = UNHANDLED.search(line)
        if m:
            a = CALL_ACTION.search(m.group(1))
            pending_action = a.group(1) if a else "?"
            continue
        m = OCPP_ERROR.match(line.strip())
        if m and pending_action is not None:
            unhandled.append((pending_action, m.group(1).split(".")[-1], m.group(2)))
            pending_action = None
    return received, unhandled


def main() -> int:
    ap = argparse.ArgumentParser(description="E6 interoperability verdict")
    ap.add_argument("--events", required=True, type=Path)
    ap.add_argument("--server-log", type=Path, default=None)
    args = ap.parse_args()

    per = scan_events(args.events)
    received, unhandled = (
        scan_server_log(args.server_log) if args.server_log else ({}, [])
    )

    if not per:
        print("No external station found in the event log. Did the simulator "
              "connect to THIS server, and is its id something other than CP####?")
        return 1

    ok = True
    for sid, r in sorted(per.items()):
        print(f"\n=== {sid}  ({r['vendor_model'] or 'no boot seen'})")
        checks = [
            ("connected", r["connections"] > 0, r["connections"]),
            ("boot accepted", r["booted"] > 0, r["booted"]),
            ("authorized", r["authorize"].get("Accepted", 0) > 0, dict(r["authorize"])),
            ("transaction started", r["tx_started"] > 0, r["tx_started"]),
            ("meter updates", r["tx_updated"] > 0, r["tx_updated"]),
            ("transaction ended", r["tx_ended"] > 0, r["tx_ended"]),
        ]
        for name, passed, detail in checks:
            ok &= passed
            print(f"  [{'PASS' if passed else 'FAIL'}] {name:20} {detail}")
        print(f"  energy delivered     {r['energy_wh']:.1f} Wh")
        if r["unrecognised"]:
            print(f"  measurands not stored (reported, not dropped): "
                  f"{sorted(r['unrecognised'])}")
        if sid in received:
            print(f"  actions it sent      {dict(received[sid])}")

    if args.server_log:
        print("\n=== requests the CSMS could not handle")
        if not unhandled:
            print("  none")
        for action, err, msg in unhandled:
            print(f"  {action:28} {err}: {msg[:100]}")
        # An unimplemented OPTIONAL message is a finding, not a failure; a
        # schema violation in a message we DO implement is a real defect.
        core = {"BootNotification", "Heartbeat", "StatusNotification",
                "Authorize", "TransactionEvent"}
        if any(a in core for a, _, _ in unhandled):
            ok = False
            print("  ^ a CORE message failed -- that is an E6 failure, not a finding")
    else:
        print("\n(no --server-log given: unhandled requests not checked)")

    print(f"\nE6 {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
