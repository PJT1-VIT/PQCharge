"""
E1 — HANDSHAKE COST: how long does it take a charger to connect securely?

Track C (analysis). Phase C6.

--------------------------------------------------------------------
IN PLAIN WORDS

Before a charger can say anything, it performs a HANDSHAKE: open a network
connection, agree encryption (TLS -- the padlock in a browser), show its
certificate, and switch to the charging protocol. Post-quantum security
makes the keys and signatures bigger, so the handshake may get slower and
heavier. E1 measures exactly how much.

Two timers exist, and they measure DIFFERENT things:

    station-side   (the headline)  measured by the charger itself, from
                   "dial" to "ready": TCP + TLS + WebSocket upgrade.
                   Recorded by the agent since the C5 fix (connect_ms);
                   since C6.2 also written the moment each connection
                   opens (station_connected), so Ctrl-C does not lose it.

    server-side    (a cross-check) measured by the CSMS, which can only see
                   the last part -- the WebSocket upgrade. Track A records
                   this scope in every line (handshake_scope) precisely so
                   nobody mistakes it for the full figure.

A charger's FIRST connection is the handshake E1 is about. Later ones are
reconnections after an outage, reported separately because they happen
under a reconnection storm (E2), not in calm conditions.

Also reported: bytes exchanged per connection (post-quantum keys cost
bytes, not just time) and the TLS version/cipher actually negotiated.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from analysis import stats
from analysis.match import MatchedRun


def measure(run: MatchedRun) -> dict[str, Any]:
    initial: list[float] = []
    reconnect: list[float] = []
    have_station_side = False

    # C6.2: from station_connected lines and/or station_finished rows, so a
    # run stopped with Ctrl-C still has its connection times.
    for times in run.harness.connect_times().values():
        have_station_side = True
        if times:
            initial.append(times[0])
            reconnect.extend(times[1:])

    established = run.events("connection_established")
    server_ms = [e["handshake_ms"] for e in established if e.get("handshake_ms") is not None]

    closed = [
        e for e in run.events("connection_closed")
        if e.get("bytes_tx") is not None and e.get("bytes_rx") is not None
    ]
    per_conn_bytes = [float(e["bytes_tx"]) + float(e["bytes_rx"]) for e in closed]

    tls_versions: Counter[str] = Counter()
    ciphers: Counter[str] = Counter()
    scopes: Counter[str] = Counter()
    cert_bytes: list[float] = []
    for e in established:
        p = e.get("payload") or {}
        if p.get("tls_version"):
            tls_versions[str(p["tls_version"])] += 1
        if p.get("tls_cipher"):
            ciphers[str(p["tls_cipher"])] += 1
        if p.get("handshake_scope"):
            scopes[str(p["handshake_scope"])] += 1
        if p.get("peer_cert_bytes") is not None:
            cert_bytes.append(float(p["peer_cert_bytes"]))

    def top(c: Counter[str]) -> str | None:
        return c.most_common(1)[0][0] if c else None

    return {
        "station_side_available": have_station_side,
        "station_connect_ms": stats.describe(initial),
        "reconnect_connect_ms": stats.describe(reconnect),
        "server_upgrade_ms": stats.describe(server_ms),
        "server_scope": top(scopes),
        "bytes_per_connection": stats.describe(per_conn_bytes),
        "bytes_tx_mean": (sum(float(e["bytes_tx"]) for e in closed) / len(closed)) if closed else None,
        "bytes_rx_mean": (sum(float(e["bytes_rx"]) for e in closed) / len(closed)) if closed else None,
        "tls": {
            "version": top(tls_versions),
            "cipher": top(ciphers),
            "client_cert_bytes_median": stats.percentile(cert_bytes, 50) if cert_bytes else None,
        },
    }
