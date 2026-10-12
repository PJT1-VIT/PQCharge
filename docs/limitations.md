# PQCharge — Limitations and Technical Refinements

A running record, updated as issues are encountered. Do not
reconstruct this at the end — contemporaneous entries read as rigour,
reconstructed ones read as vague. Each entry: the issue, and the
corrective action taken.

---

## Refinements made during design

### R1 — Certificate rotation cannot preserve the TCP connection
TLS 1.3 removed renegotiation, so a station cannot swap its client
certificate on a live connection. The original "rotation without a
dropped connection" is not achievable as stated.

**Corrective action.** The continuity guarantee was moved to the
application layer: overlapping certificate validity ensures either
certificate authenticates the station during transition, and the
requirement became *no lost charging transaction* rather than an
unbroken socket.

### R2 — Automatic fallback to classical TLS is a downgrade surface
An adversary able to induce latency or packet loss could force
stations to fall back to classical cryptography, which the threat
model assumes to be compromisable.

**Corrective action.** Fallback constrained to a declared migration
window, rate-limited per station and per fleet, and logged so repeated
triggering is visible as a potential attack rather than routine
degradation.

### R3 — OQS provider does not load into Windows CPython
OpenSSL 3.x with the OQS provider does not load cleanly into Python's
ssl module on Windows, since CPython ships a bundled OpenSSL that does
not accept custom providers.

**Corrective action.** The transport decision was placed behind the
crypto provider interface, so an in-TLS or an application-layer
post-quantum handshake satisfies the same contract, with a timeboxed
decision point at Stage 4.



### R4 — Certificates carried no Subject Alternative Name
crypto/ca.py first issued certificates with Common Name only. Python's
ssl module accepted this via OpenSSL's CN-fallback (mutual TLS handshake
tested end to end, Day 6), but strict TLS stacks require a SAN, and the
Raspberry Pi dials the laptop by IP address, which a CN cannot match.

**Corrective action (Track B, R2 and B-F3, 2026-10-10).** The server
certificate carries a SAN: "localhost" plus every name or IP given to
`bootstrap_pki --server-san` (IP addresses are written as IPAddress
entries, names as DNSName). Every certificate also gained Authority and
Subject Key Identifiers and BasicConstraints (L06), which Python 3.13's
strict verification requires. Station certificates keep the station id
as Common Name, which is what the server's identity check compares.

### R5 — Windows-specific socket teardown race in test harnesses
A TLS test client closing its socket immediately after reading the
peer certificate can trigger WinError 10053 on the server side, if the
server hasn't finished its own read before the closure arrives. Not
present on Linux in the same code. Fixed by an explicit one-byte
acknowledgment exchanged over the TLS channel before either side
closes, rather than relying on timing.

---

## Limitations encountered during implementation

_(Add entries here as they arise, Days 3 onward.)_

### I1 — Two challenges for one charger cancelled each other (L35)
The authenticator held one outstanding challenge per station, so a boot
check and a migration check for the same charger overwrote each other
and both failed.

**Corrective action (Track B, B-P2, 2026-10-10).** Up to 8 outstanding
challenges per station, each verified by its own nonce.

### I2 — Key rotation had no overlap window (L26)
Replacing a charger's key in one step meant a failed rotation left it
with no valid key.

**Corrective action (Track B, B-F2, 2026-10-10).** The new key is staged
next to the old one, proven with a challenge naming the new key id, and
only then committed; on failure the old key stays, and a wave over the
failure threshold restores every old key of that wave. Checked live:
3 chargers rotated while charging, no session closed.

### I3 — ML-DSA certificate sizes could not be measured (L43)
The installed `cryptography` could not build ML-DSA X.509 certificates,
so E4 had no post-quantum certificate rows.

**Corrective action (Track B, B-F5, 2026-10-12).** With `cryptography`
50+ the rows come from real ML-DSA certificates verified against the
root; otherwise they are computed from FIPS 204 sizes plus measured
X.509 overhead and labelled "(computed)" (within 8 bytes of the real
sizes, tested). Result: an ML-DSA-44 certificate fits OCPP's 5,500-
character limit; the station + root chain does not fit 10,000.

### I4 — The Pi's sensor reads milliwatts, not kilowatts (L32, L37)
The INA219 measures the LED circuit (tens of mW), which is invisible next
to simulated 7.4 kW chargers.

**Corrective action (Track B, B-F8 + B-F10).** The Pi reports scaled
power (LED duty x 7.4 kW) as its OCPP power value, and sends the real
INA219 reading alongside it as OCPP customData (`pqcharge.hw`), so the
scaling is visible and checkable. A second power sample in mW was
rejected: the server takes the last power sample and would read the mW
number as watts.

### I5 — The server made each charger's private key (L01)
Before Contract 7 the orchestrator generated each charger's ML-DSA key
pair and sent the private key over the network (InstallPQAuth).

**Corrective action (Contract 7; Track B B-P1 and B-F1b, PR #32, #49).**
The charger makes its own key and sends only the public key
(RequestPQEnrolment); the orchestrator no longer has the old path.
