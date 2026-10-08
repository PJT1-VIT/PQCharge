# PQCharge — Open Items and Limitations

**The single register of everything not yet resolved.** Started 2026-10-03, after the A+B+C integration. **Updated 2026-10-08:** every team item is agreed by A, B and C. **Contract 7 is frozen** (approved by A, B, C), so L03 (meaning of the modes) and L18 (contract approval) are resolved and removed.

## Rules (all three tracks)

1. **Add** any discrepancy, finding or limitation that is:
   - not fixed yet,
   - planned for later, or
   - waiting for a team decision.

   Do this **before** your next commit. Give it the next free `L` number and never reuse a number.
2. **Remove** an item in the same commit/PR that resolves it.
   - If the reason is worth keeping for the report, add one "issue + corrective action" entry to `docs/limitations.md`, the permanent design record.
3. **Before planning or implementing anything,** every track reads this file and the other two tracks' dev plans.
4. **Status values:**
   - **Agreed** — decided; the owner does it.
   - **Not decided** — needs a team decision; the action shown is only the recommendation.
   - **Planned** — already scheduled in the owner's dev plan.
   - **Accepted** — a known limit we keep; it is stated in the report and stays here.
5. **Team items** are agreed by all three tracks (confirmed 2026-10-08). New team items start as "Proposed" until all three agree.

Old numbers (F1, F13, F14, R11, …) differed between the dev plans; F14 meant two different things. **From now on, cite the `L` number.**

---

## 1. Blocking E1 / E2 (all decided; built through Contract 7)

| No. | Finding | Status | Action | Owner |
|---|---|---|---|---|
| **L01** | **The server makes every charger's private key and sends it over the network.** The charger keeps it in memory only, so a restart loses it. *(old: F2+F3, A-F13, B-F13)* | **Agreed (A, B, C)** | The charger generates its own ML-DSA key, sends **only the public key**, and saves the private key in `certs/pq/<id>.json` (ignored by git). **Specified in Contract 7 §7.2–7.4.** | B (orchestrator install step), C (`agent/pqc_messages.py`, `agent/pq_identity.py`); A reviews |
| **L02** | **The security mode is only a label.** The key is checked once, at migration, so "classical" and "post-quantum" chargers connect in exactly the same way. E1/E2 would compare identical things. *(old: F1, A-8)* | **Agreed (A, B, C)** | The server challenges every migrated charger after **each** accepted boot (Track A's boot listener); a failed check **closes the connection** (code 1008). **Specified in Contract 7 §7.5–7.7.** **E2 "recovered" in post-quantum mode = connected + boot accepted + key check passed.** **Presentation:** the migration must be visibly obvious to the panel (dashboard migration timeline, live ticker of waves and key checks, a readable console view). | A (boot hook, recovered rule), B (challenge on boot), C (agent; display) |

## 2. Agreed fixes, not yet done

| No. | Finding | Status | Action | Owner |
|---|---|---|---|---|
| L04 | **Private keys appear in the server log.** The `ocpp` library logs every `InstallPQAuth` message at INFO, key included. GitGuardian flagged the copies once committed in `evidence/`. *(old: A-F14)* | **Agreed (A, B, C)** | 1. Track A adds a log filter that blanks `private_key` values. 2. Never commit `*.log`. 3. Koshambi closes the GitGuardian incident as a test credential. 4. No git-history rewrite (throwaway test keys). **Root fix: Contract 7 removes `InstallPQAuth` (§7.8).** | A (filter), C (incident) |
| L05 | **Coarse timers on Windows before Python 3.13.** `time.monotonic()` moves in ~15.6 ms steps. The agent is fixed (PR #26); Track A's timers (`monotonic_ns`: server handshake time, key-check round trip) are not. | **Agreed (A, B, C)** | **All tracks move to Python 3.13+.** Remove this item once all three confirm they are on 3.13. | A, B, C |
| L06 | **Certificates lack the AKI/SKI extensions.** Strict TLS software rejects them; Python 3.13's default context did in session Step 1b. *(old: integration-plan F14)* | Agreed | Add both extensions in `crypto/ca.py`, then regenerate all certificates. | B |
| L07 | **The identity check only warns by default.** | Agreed | Default `--tls-identity-check` to `enforce`. The agent already passed under `enforce` in session Step 1b. | A |
| L09 | **E3 showed the rollback but not the halt.** The failing chargers were in the last wave. | Agreed | One extra E3 run with the refusers mid-fleet (for example CP0021–25): a new fleet profile, plus the load generator run twice with `--start-index`. | C runs; B (profile contents), A (fixture) |
| L11 | **Session runs were stopped with Ctrl-C,** so they are marked "incomplete". | Agreed | Every run ends on its own. `--charge-for` follows the rule in Track C's plan (ramp + action + 30 s observation + 30 s margin). | C |

## 3. Planned (in the owner's plan)

| No. | Finding | Status | Action | Owner |
|---|---|---|---|---|
| L14 | **Contract 3 lacks `station_deferred` and `migration_failed`.** The orchestrator already emits both; Track C's analysis already reads them. *(old: integration-plan F7, A-6)* | Planned | Add both to `EventType` in `csms/events.py` (a frozen contract: tell B and C in the PR). | A |
| L15 | **The event log fsyncs every event,** which may slow the server at fleet scale and distort E2 timings. | Planned | fsync experiment at N=50/100 before E2 (Track A plan §8). | A |
| L16 | **E6 over `wss://` is not yet shown.** | Planned | `bootstrap_pki --also E6-SAP-01`, then re-run E6 over TLS. | A + B |

## 4. Not decided

| No. | Finding | Status | Action | Owner |
|---|---|---|---|---|
| L08 | **Under TLS, the tester's fleet watcher borrows CP0001's certificate** to call `/api` (no operator certificate exists). **Not verified whether the identity check inspects `/api` requests.** | Not decided | *Recommended:* Track B issues one operator certificate. | B, C |
| L12 | **E6's second half is not done:** our agent against a third-party CSMS. The first half (a third-party charger on our server) passed on Day 10. | Not decided | *Recommended:* compare CitrineOS and MaEVe (both OCPP 2.0.1, open source) on install effort, then pick one. **Install effort not yet investigated.** | C + A |
| L13 | **Raspberry Pi hardware status unknown** (Pi, relay, INA219 sensor). | Not decided | Confirm what is bought. On the Pi: Track A's quantcrypt 1312 check; certificate SAN for the laptop's `.local` name (old B-R4). | Team; C (C9) |

## 4b. New items (2026-10-08)

| No. | Finding | Status | Action | Owner |
|---|---|---|---|---|
| **L17** | **Pure PQC may not run in Windows Python.** Our design record (`docs/limitations.md` R3) and Track B's plan say liboqs's OpenSSL provider does not load into Windows CPython. Pure-PQC runs may then need Linux (WSL or the Pi), a different platform from the other two modes, which affects the fairness of E1/E2. **Not yet tested with Python 3.13 / current OpenSSL.** | Not decided | *Recommended:* Track B runs a timeboxed test (2 days, as the design document's original transport decision did). On Windows: does Python 3.13's `ssl` load the oqs provider (or a native ML-KEM/ML-DSA OpenSSL)? If not, what works on WSL/Linux? Then decide where **all three** modes run, so the comparison stays like for like. Contract 7 v2 then fixes `pqc`. | B leads; A, C |
| **L19** | **Charger key files cannot be locked down on Windows** (no `chmod 0600`). | Proposed (accepted limitation) | State it in the report. Simulated chargers only; the Pi (Linux) uses mode 0600. | C |

## 5. Accepted limitations (stay in the report)

| No. | Limitation | Why it is accepted |
|---|---|---|
| L20 | **Post-quantum protection is at the application layer only (Option B).** TLS stays classical X.509, so the OCPP traffic itself is not post-quantum encrypted. | Post-quantum TLS cannot be loaded into Windows CPython (docs R3), and the installed `cryptography` cannot make ML-DSA certificates. Keeps third-party chargers (E6) working. |
| L21 | **Write-behind persistence:** a hard kill of the server loses up to one flush interval (default 2 s) of station state and of enrolled keys. *(A-F4)* | A trade-off for speed at fleet scale. Measured and stated. |
| L22 | **Every `/api` endpoint is GET,** including ones that change things. *(A-F7)* | The `websockets` HTTP parser rejects other methods. Mitigation: state-changing GETs sit behind mutual TLS. |
| L23 | **quantcrypt 1.0.0 can install without its engine** (seen on an Intel Mac with Python 3.11). *(A-F8)* | The server probes key generation at startup and disables migration with a reason instead of crashing. Run the 1312 check on every machine. |
| L24 | **The SAP simulator's energy numbers are not physical,** and it does not implement SetChargingProfile in 2.0.1 mode. *(A-F10, A-F11)* | It is used only for E6 interoperability; its energy never enters an energy result. Third-party curtailment is unproven (EVerest optional). |
| L25 | **The agent's offline queue holds at most 2,000 events;** older ones are dropped when full. | Bounded memory at 500 chargers; drops are counted and reported by C6. |

---

*Contract 7 (security modes) is in the project doc `PQCharge_Interface_Contracts.md`.*

*Resolved and removed:*

| Item | Fixed in |
|---|---|
| Ctrl-C lost E1 data (old C-1) | PR #24 (C6.2) |
| Agent connection timer | PR #26 |
| Stubs | PR #23 |
| Server logs in evidence | PR #25 |
| L03 (meaning of the three modes) | Contract 7 §7.1, frozen 2026-10-08 |
| L18 (Contract 7 approval) | approved by A, B, C, 2026-10-08 |
| L10 (first key check ~10× slower) | C-P2: the provider is built once at load-generator start, and at charger start when a key file exists (Contract 7 §7.3) |

**Note:** `docs/limitations.md` R4 ("certificates carry no SAN") is out of date: the SAN mechanism was merged (Track B R2). Track B should update it.
