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

---

## Limitations encountered during implementation

_(Add entries here as they arise, Days 3 onward.)_