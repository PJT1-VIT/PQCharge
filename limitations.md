# PQCharge — Open Items and Limitations

**The single register of everything not yet resolved.** Started 2026-10-03, after the A+B+C integration. **Updated 2026-10-09:** fleet-migration audit added L26–L29. **Later, 2026-10-09 (Track B, B-P1):** L01 progress, L08 verified, L31 added. **Earlier, 2026-10-08:** every team item is agreed by A, B and C. **Contract 7 is frozen** (approved by A, B, C), so L03 (meaning of the modes) and L18 (contract approval) are resolved and removed.

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
| **L01** | **The server makes every charger's private key and sends it over the network.** The charger keeps it in memory only, so a restart loses it. *(old: F2+F3, A-F13, B-F13)* | **Agreed (A, B, C)** | The charger generates its own ML-DSA key, sends **only the public key**, and saves the private key in `certs/pq/<id>.json` (ignored by git). **Specified in Contract 7 §7.2–7.4.** **Progress 2026-10-09:** Track C's side merged (C-P1, C-P2). Track B's side done (B-P1): the orchestrator takes `enrolment_request_factory` + `public_key_parser`; live check with Track C's builders and agents: 3 migrated, 1 incompatible, key files only on the chargers, no `private_key` in the server log or diary. ~~**Remaining:** Track A switches `csms/migration.py` to the new options (A-P4)~~ **Track A side done (A-P4, Track A PR 2, 2026-10-10):** `csms/migration.py` passes `enrolment_request_factory=build_enrolment_request` and `public_key_parser=lambda r: parse_enrolment_reply(r.data)`; live check with 3 real agents in `--mode hybrid`: 3 migrated, 1 incompatible, matching `key_id` on every `pq_enrolled`/`pq_auth` line, server log shows 3 `RequestPQEnrolment`, **0** `InstallPQAuth`, **0** `private_key`; Track A's refuser fixture still rolls its wave back. **Remaining:** the A+B+C re-test (C-P8); then `InstallPQAuth` is removed (A-P6, B-P5). | B (orchestrator install step), C (`agent/pqc_messages.py`, `agent/pq_identity.py`); A reviews |
| **L02** | **The security mode is only a label.** The key is checked once, at migration, so "classical" and "post-quantum" chargers connect in exactly the same way. E1/E2 would compare identical things. *(old: F1, A-8)* | **Agreed (A, B, C)** | The server challenges every migrated charger after **each** accepted boot (Track A's boot listener); a failed check **closes the connection** (code 1008). **Specified in Contract 7 §7.5–7.7.** **E2 "recovered" in post-quantum mode = connected + boot accepted + key check passed.** **Presentation:** the migration must be visibly obvious to the panel (dashboard migration timeline, live ticker of waves and key checks, a readable console view). **Progress 2026-10-10 (Track A PR 2):** A-P1 boot hook (`registry.add_boot_listener`, fired from `@after("BootNotification")` on its own task) and A-P2 (`StationView.pq_verified` + `pq_check_required`, per-mode `is_recovered`) are built and tested. **Remaining:** B-P2 boot verifier, then A-P3 (`--mode hybrid` registers it; close 1008 on failure). | A (boot hook, recovered rule), B (challenge on boot), C (agent; display) |

## 2. Agreed fixes, not yet done

| No. | Finding | Status | Action | Owner |
|---|---|---|---|---|
| L04 | **Private keys appear in the server log.** The `ocpp` library logs every `InstallPQAuth` message at INFO, key included. GitGuardian flagged the copies once committed in `evidence/`. *(old: A-F14)* | **Agreed (A, B, C)** | 1. Track A adds a log filter that blanks `private_key` values. 2. Never commit `*.log`. 3. Koshambi closes the GitGuardian incident as a test credential. 4. No git-history rewrite (throwaway test keys). **Root fix: Contract 7 removes `InstallPQAuth` (§7.8).** **Step 1 done (Track A, 2026-10-10):** `csms/log_redaction.py` blanks every `private_key` value in the server's output log, including the escaped form inside `DataTransfer.data` and tracebacks; checked live (3 `InstallPQAuth` sends logged, 3 values `[REDACTED]`). Item stays open until `InstallPQAuth` is removed (A-P6 / B-P5). | A (filter ✔), C (incident) |
| L05 | **Coarse timers on Windows before Python 3.13.** `time.monotonic()` moves in ~15.6 ms steps. The agent is fixed (PR #26); Track A's timers (`monotonic_ns`: server handshake time, key-check round trip) are not. | **Agreed (A, B, C)** | **All tracks move to Python 3.13+.** Remove this item once all three confirm they are on 3.13. **Track A: on 3.13 (Mac, 3.13.16, 2026-10-10; full suite passes).** | A, B, C |
| L06 | **Certificates lack the AKI/SKI extensions.** Strict TLS software rejects them; Python 3.13's default context did in session Step 1b. *(old: integration-plan F14)* | Agreed | Add both extensions in `crypto/ca.py`, then regenerate all certificates. | B |
| L09 | **E3 showed the rollback but not the halt.** The failing chargers were in the last wave. | Agreed | One extra E3 run with the refusers mid-fleet (for example CP0021–25): a new fleet profile, plus the load generator run twice with `--start-index`. | C runs; B (profile contents), A (fixture) |
| L11 | **Session runs were stopped with Ctrl-C,** so they are marked "incomplete". | Agreed | Every run ends on its own. `--charge-for` follows the rule in Track C's plan (ramp + action + 30 s observation + 30 s margin). | C |

## 3. Planned (in the owner's plan)

| No. | Finding | Status | Action | Owner |
|---|---|---|---|---|
| L15 | **The event log fsyncs every event,** which may slow the server at fleet scale and distort E2 timings. | Planned | fsync experiment at N=50/100 before E2 (Track A plan §8). | A |
| L16 | **E6 over `wss://` is not yet shown.** | Planned | `bootstrap_pki --also E6-SAP-01`, then re-run E6 over TLS. | A + B |

## 4. Not decided

| No. | Finding | Status | Action | Owner |
|---|---|---|---|---|
| L08 | **Under TLS, the tester's fleet watcher borrows CP0001's certificate** to call `/api` (no operator certificate exists). **Verified 2026-10-09 (Track B, code read of `csms/server.py`):** the identity check does **not** inspect `/api`. `process_request` answers every `/api/` path itself; `check_identity` runs only in the WebSocket handler, which `/api` never reaches. With `--tls-client-certs required` the TLS layer still demands a certificate signed by our CA, so **any** station's certificate opens every `/api` endpoint, including `/api/migration/start`. | Not decided | *Recommended:* Track B issues one operator certificate. | B, C |
| L12 | **E6's second half is not done:** our agent against a third-party CSMS. The first half (a third-party charger on our server) passed on Day 10. | Not decided | *Recommended:* compare CitrineOS and MaEVe (both OCPP 2.0.1, open source) on install effort, then pick one. **Install effort not yet investigated.** | C + A |
| L13 | **Raspberry Pi charger node: setup checks still open.** Hardware is **agreed and bought (2026-10-09):** Raspberry Pi + INA219 sensor + an LED with a 330 Ω resistor on **GPIO18 (PWM)**, no relay. The LED stands for the charging power: on/off = contactor closed/open, brightness (PWM duty) = power limit. | Agreed (hardware); checks open | On the Pi: Track A's quantcrypt 1312 check; certificate SAN for the laptop's `.local` name (old B-R4). Track C builds `GPIOPower` (C9) behind Contract 5. How the INA219 reading becomes an OCPP meter value: **L32**. | C (C9); A + B (checks) |

## 4b. New items (2026-10-08)

| No. | Finding | Status | Action | Owner |
|---|---|---|---|---|
| **L17** | **Pure PQC may not run in Windows Python.** Our design record (`docs/limitations.md` R3) and Track B's plan say liboqs's OpenSSL provider does not load into Windows CPython. Pure-PQC runs may then need Linux (WSL or the Pi), a different platform from the other two modes, which affects the fairness of E1/E2. **Not yet tested with Python 3.13 / current OpenSSL.** | Not decided | *Recommended:* Track B runs a timeboxed test (2 days, as the design document's original transport decision did). On Windows: does Python 3.13's `ssl` load the oqs provider (or a native ML-KEM/ML-DSA OpenSSL)? If not, what works on WSL/Linux? Then decide where **all three** modes run, so the comparison stays like for like. Contract 7 v2 then fixes `pqc`. | B leads; A, C |
| **L19** | **Charger key files cannot be locked down on Windows** (no `chmod 0600`). | Proposed (accepted limitation) | State it in the report. Simulated chargers only; the Pi (Linux) uses mode 0600. | C |
| **L26** | **Key rotation of already-migrated chargers has no overlap window** (fleet-migration piece M5, the design document's "mid-session rotation" claim). `PQAuthenticator` holds one key per charger, so re-running a migration overwrites the old key before the new one is proven; a failure leaves the charger with no post-quantum identity. The orchestrator itself says rotation "is added later". | Agreed (owner) | Track B builds a rotation flow: keep old + new key during the window, challenge with the new `key_id`, drop the old key only after it verifies, keep the old key on failure (B-P6). Track A adds the trigger endpoint (A-P8); Track C does the analysis and display (C-P11). The charger side is ready (C-P2 keeps the previous key). | B (A, C) |
| **L27** | **A server restart in the middle of a migration is untested** (M9). Per-charger migration state and enrolled keys survive a restart; the running migration task and its wave status live in memory only. | Not decided | Track B tests it (B-T1) and decides the behaviour (for example: an interrupted migration is reported as `failed`, and in-progress chargers return to `pending`). | B (A) |
| **L28** | **Migration target label mismatch** (M6): `/api/migration/start` accepts only `target_mode=pqc`, but under Contract 7 an enrolled charger is in **hybrid** mode. | Proposed | Accept `target_mode=hybrid` for the ML-DSA enrolment migration; keep `pqc` for the pure-PQC flow. Track A (A-P7) and Track B (B-P7) change it together. | A + B |
| **L29** | **The pure-PQC migration flow is not designed** (M7): switching chargers to post-quantum TLS certificates in waves, with rollback. | Not decided | After the liboqs test (L17), Track B designs it as Contract 7 v2 (B-P8). Then Track A adds post-quantum TLS on the server (A-P9) and Track C the charger TLS (C-P10). | B (A, C) |
| **L30** | **The charger cannot tell a boot key check from any other challenge.** The analysis (C-P4) counts the *first* challenge of a connection that opened with a key held as the boot check (E1 secure-ready). If the server ever sends a different challenge first on such a connection (for example a rotation, L26), it would be counted as secure-ready. Today the boot check is the only such challenge, and a hybrid run with keys but no boot checks is already flagged (`hybrid_without_boot_checks`). | Proposed | Contract 7 v2: add an optional `reason` (`boot` / `migration` / `rotation`) to `PQAuthChallenge`; the charger copies it into `station_authenticated`; the analysis then uses it instead of "first challenge". Decide together with the rotation flow (B-P6). | C (B) |

## 4c. New items (2026-10-09)

| No. | Finding | Status | Action | Owner |
|---|---|---|---|---|
| **L31** | **Where `source` in the post-quantum events comes from.** Contract 7 §7.6 lists `source: "orchestrator"\|"boot_verifier"` in the `pq_auth` payload. In code, Track B's orchestrator must **not** put `source` in a payload: Track A's `orchestrator_emitter` (`csms/migration.py`) already adds `source="orchestrator"`, and a second `source` keyword raises `TypeError` inside `EventLog.emit` (verified 2026-10-09). B-P1 follows this, and the live diary shows `source: "orchestrator"` on every `pq_enrolled` and `pq_auth` line. **Open for B-P2:** the boot verifier's `source: "boot_verifier"` must come the same way, from the emitter Track A builds for it (A-P3). | **Agreed (A, B)** | Track A's emitter for the boot verifier writes `source="boot_verifier"`; Track B's verifier never puts `source` in its payload. Contract 7 text unchanged (the event shape is the same); add one sentence to §7.6 at the next revision. **Approved by Track A (2026-10-10).** Track A PR 2: `orchestrator_emitter(event_log, source=...)` is the one emitter for both (A-P3 builds it with `source="boot_verifier"`), and a `source` passed by a caller is dropped with a warning instead of raising `TypeError` and losing the event. Track C informed. | A + B |
| **L32** | **The Pi's power reading is milliwatts, not kilowatts.** The INA219 measures the LED circuit (tens of mW), but OCPP meter values, the fleet power line and E5's curtailment chart expect a believable charger (up to 7.4 kW). Two options: **(a)** report a **scaled** value, e.g. PWM duty × 7.4 kW, labelled "scaled" (the INA219 then proves the LED really changed); **(b)** report the **real** milliwatts. With (b) the Pi's power is invisible next to 499 simulated 7.4 kW chargers; with (a) the number is not a physical measurement. | Not decided | *Recommended:* (a), with the raw INA219 mW reported alongside so the scaling is visible and checkable. Either way the Pi's energy is labelled (as for E6 nodes, `energy_source: "charger-reported"`) and never presented as a measured 7.4 kW. Decide before C9. | Team; C (C9) |
| **L33** | **Tester diaries get too big for git at fleet scale.** In the S1 runs (2026-10-10) 98 % of each tester diary is `fleet_snapshot` lines (the whole fleet, once a second, from `--watch-fleet`): 4.7 MB at 50 chargers, 23 MB at 250, **46 MB at 500**. An E2 storm run at 500 chargers (`--charge-for 240`) would be about **90–100 MB**, at GitHub's 100 MB per-file limit, and every evidence folder stays in the repo history for good. | Proposed | *Recommended:* Track C lets the analysis read gzip-compressed diaries (`*.jsonl.gz`, typically ~10× smaller) and the evidence rule becomes "copy `*.jsonl` and `*.json`, gzip any file over 10 MB". Until then: S1 evidence is committed as plain `*.jsonl` (every file < 50 MB); for E2 at 500, use `--watch-every 2`. Needs A and B to agree to the evidence-rule change. | C (A, B agree) |

## 4d. New items (2026-10-10, Track A)

| No. | Finding | Status | Action | Owner |
|---|---|---|---|---|
| **L34** | **A charger verified by its migration check counts as recovered (gap in Contract 7 §7.5/§7.7).** §7.7 sets `pq_verified` only from the **boot** check, so in `--mode hybrid` every charger migrated on its current connection would become enrolled (`pq_check_required`) but unverified, and show as **not recovered** until it reconnected. | Proposed (Track A; needs B and C) | **Built in Track A PR 2:** a **passed** migration key check (`pq_auth`, `result: success`) also marks that charger's current connection `pq_verified` (`orchestrator_emitter(on_pq_auth_success=registry.mark_pq_verified)`). Same evidence as a boot check: a fresh nonce signed with the enrolled key, on the same connection. Live: 3 migrated in hybrid, all 3 stayed recovered. Add one sentence to §7.7 at the next contract revision. Remove when B and C agree. | A (B, C agree) |

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
| L07 (identity check only warned by default) | Track A PR 1 (2026-10-10): `--tls-identity-check` defaults to `enforce`; `warn` still available explicitly |
| L14 (Contract 3 lacked two event types) | Track A PR 1 (2026-10-10): `EventType.STATION_DEFERRED`, `EventType.MIGRATION_FAILED` (same strings as before) |

**Note:** `docs/limitations.md` R4 ("certificates carry no SAN") is out of date: the SAN mechanism was merged (Track B R2). Track B should update it.