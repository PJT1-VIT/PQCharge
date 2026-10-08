"""
Live certificate rotation — what happened, read from the CSMS's own event log.

Track B. Phase 5 evidence. Reads a Contract 3 events file (the CSMS's --log)
and prints, per station: every connection's key exchange group and certificate
type, every rotation attempt, every transaction id, and whether any private
key ever appeared on the CSMS side.

    python -m experiments.rotation_report logs\\phase5\\events.jsonl [--server-log logs\\phase5\\server.txt]
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("events", help="the CSMS's --log file (events.jsonl)")
    ap.add_argument("--server-log", default=None, help="the CSMS's stderr log, checked for private keys too")
    args = ap.parse_args(argv)

    events = [json.loads(line) for line in Path(args.events).read_text(encoding="utf-8").splitlines() if line.strip()]

    print("== connections, as the CSMS saw them (group / charger certificate / bytes)")
    for e in events:
        p = e.get("payload") or {}
        if e["event_type"] == "connection_established":
            print(f"  {e['station_id']}  {p.get('tls_group')}  {p.get('peer_key_type')}  {p.get('peer_cert_bytes')}")

    print("== migration")
    for e in events:
        p = e.get("payload") or {}
        t = e["event_type"]
        if t == "connection_attempt" and p.get("transition") == "cert_rotation":
            print(f"  rotation {p.get('station')}: {p.get('result')} at step '{p.get('step')}' "
                  f"in {round(p.get('duration_ms') or 0, 1)} ms -- {p.get('detail')}")
        elif t == "connection_attempt" and p.get("transition") == "certificate_refused":
            print(f"  refused  {e['station_id']}: rolled-back certificate presented")
        elif t.startswith("wave_") or t.startswith("migration_"):
            keep = {k: p[k] for k in ("method", "wave_id", "migrated", "failed", "incompatible") if k in p}
            print(f"  {t} {keep}")

    print("== transactions (one id per station = the session survived its rotation)")
    ids: dict[str, set] = collections.defaultdict(set)
    counts: collections.Counter = collections.Counter()
    for e in events:
        p = e.get("payload") or {}
        if e["event_type"].startswith("transaction_"):
            ids[e["station_id"]].add(p.get("transaction_id"))
            counts[(e["station_id"], e["event_type"])] += 1
    disturbed = 0
    for station in sorted(ids):
        n_ids = len(ids[station])
        ended = counts[(station, "transaction_ended")]
        ok = n_ids == 1 and ended == 1
        disturbed += 0 if ok else 1
        print(f"  {station}: {n_ids} transaction id(s), {counts[(station, 'transaction_updated')]} updates, "
              f"ended {ended}x  {'OK' if ok else 'DISTURBED'}")

    text = Path(args.events).read_text(encoding="utf-8")
    if args.server_log and Path(args.server_log).is_file():
        text += Path(args.server_log).read_text(encoding="utf-8", errors="replace")
    leaked = "PRIVATE KEY" in text
    print(f"== sessions disturbed: {disturbed}")
    print(f"== private key seen on the CSMS side: {'YES - FAIL' if leaked else 'no'}")
    return 1 if (leaked or disturbed) else 0


if __name__ == "__main__":
    sys.exit(main())
