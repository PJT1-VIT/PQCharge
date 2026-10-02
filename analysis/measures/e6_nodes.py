"""
E6 AND THE HARDWARE NODE — chargers the tester did not start.

Track C (analysis). Phase C6.

--------------------------------------------------------------------
IN PLAIN WORDS

Most chargers in a run are started by our load generator. Two kinds are not:

    the Raspberry Pi   the physical charger (ID CP0100, agreed), started by
                       hand on the Pi itself
    a third-party      E6: an OCPP charger program written by someone else,
    client             proving our server speaks the real protocol rather
                       than a private dialect

Neither appears in any tester diary, so they are found the other way round:
every charger the SERVER saw that no tester run started. For each one this
reports whether it connected, was accepted, completed charging sessions,
and how much energy it delivered -- the E6 pass/fail table, and the Pi's
own record.

The CSMS itself never knows which charger is physical (design rule: the
hardware node is additive). Only this report labels CP0100, from the
agreed ID.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Iterable

from analysis.collect import ServerDiary

HARDWARE_IDS = frozenset({"CP0100"})


def measure(diary: ServerDiary, harness_station_ids: Iterable[str]) -> list[dict[str, Any]]:
    started_by_tester = set(harness_station_ids)
    per: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "connections": 0, "boots": 0, "sessions_started": 0, "sessions_completed": 0,
        "energy_wh": 0.0, "first_seen": None, "last_seen": None,
        "tls_versions": Counter(), "crypto_modes": Counter(), "identity_rejected": 0,
        "pq_auth": None, "deferred": 0,
    })

    for ev in diary.events:
        sid = ev.get("station_id")
        if not sid or sid in started_by_tester:
            continue
        rec = per[sid]
        t = ev.get("timestamp")
        rec["first_seen"] = rec["first_seen"] or t
        rec["last_seen"] = t
        if ev.get("crypto_mode"):
            rec["crypto_modes"][ev["crypto_mode"]] += 1
        et = ev.get("event_type")
        p = ev.get("payload") or {}
        if et == "connection_established":
            rec["connections"] += 1
            if p.get("tls_version"):
                rec["tls_versions"][p["tls_version"]] += 1
        elif et == "state_changed" and p.get("transition") == "booted":
            rec["boots"] += 1
        elif et == "transaction_started":
            rec["sessions_started"] += 1
        elif et == "transaction_ended":
            rec["sessions_completed"] += 1
            rec["energy_wh"] += float(p.get("energy_wh") or 0.0)
        elif (et == "connection_attempt" and p.get("transition") == "identity_check"
              and ev.get("outcome") == "rejected"):
            rec["identity_rejected"] += 1
        elif et == "connection_attempt" and p.get("transition") == "pq_auth":
            # C6.1: the latest post-quantum key check for this charger
            # (result normalised into `outcome` by collect.py).
            rec["pq_auth"] = ev.get("outcome")
        elif et == "station_deferred":
            rec["deferred"] += 1

    out = []
    for sid in sorted(per):
        rec = per[sid]
        out.append({
            "station_id": sid,
            "kind": "hardware" if sid in HARDWARE_IDS else "external",
            "connected": rec["connections"] > 0,
            "accepted": rec["boots"] > 0,
            "completed_session": rec["sessions_completed"] > 0,
            "connections": rec["connections"],
            "sessions_completed": rec["sessions_completed"],
            "energy_wh": round(rec["energy_wh"], 3),
            # Track A F10: energy from a charger we did not start is what IT
            # reported (a simulator's numbers are not physical). Labelled, and
            # never added to any fleet energy total.
            "energy_source": "charger-reported",
            "identity_rejected": rec["identity_rejected"],
            "pq_auth": rec["pq_auth"],
            "deferred": rec["deferred"],
            "tls_version": rec["tls_versions"].most_common(1)[0][0] if rec["tls_versions"] else None,
            "crypto_mode": rec["crypto_modes"].most_common(1)[0][0] if rec["crypto_modes"] else None,
            "first_seen": rec["first_seen"],
            "last_seen": rec["last_seen"],
        })
    return out
