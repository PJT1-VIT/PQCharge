# PQCharge — Open Items and Limitations

**The single register of everything not yet resolved.** Started 2026-10-03, after the A+B+C integration. **Updated 2026-10-09:** fleet-migration audit added L26–L29. **Later, 2026-10-09 (Track B, B-P1):** L01 progress, L08 verified, L31 added. **2026-10-10 (Track B, B-P2):** L02 progress, Track B agrees to L34, L35 found and fixed, L36 added. **2026-10-10 (Track B, B-F1/F3/F4/F8):** L06 resolved; L28 B side done; L09 profile ready; L32 decided and built; L17, L29, L27, L08 moved to Accepted (FINAL 2-DAY PLAN). **B-F2:** L26 resolved (rotation with overlap window). **2026-10-11 (Track C):** work tracker added (§0); L38–L43 added; L13 and L37 updated. **2026-10-12 (Track B, B-F5 + B-F10):** L43 decided and closed; L37 decided (customData), Track B half built. **2026-10-12 (Track B, B-F1b + B-F9 prep):** orchestrator side of `InstallPQAuth` removed (merge after step 9); L42 updated; Pi set-up script. **Earlier, 2026-10-08:** every team item is agreed by A, B and C. **Contract 7 is frozen** (approved by A, B, C), so L03 (meaning of the modes) and L18 (contract approval) are resolved and removed.

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
6. **The work tracker (§0) is updated in every PR:** tick ✅ the task the PR finishes, with the PR number, and remove the L items it closes.

Old numbers (F1, F13, F14, R11, …) differed between the dev plans; F14 meant two different things. **From now on, cite the `L` number.**

---

## 0. Work tracker — everything left until `v1.0` (all tracks, in order)

**Added 2026-10-11 (Track C)**, checked against `main` = `199983a` (PR #46) and the FINAL 2-DAY PLAN in the three dev plans. The dev plans are not fully up to date; **this table is the one place to track the work.**

**Goal:** the whole fleet migrating live, while charging, on the dashboard; the Raspberry Pi as a real charger; classical vs hybrid compared on the results page.

**How to read it:**

- Stages run top to bottom. A task needs everything under **Needs first**. Tasks in the same stage that do not depend on each other can run at the same time.
- **Tests and integration come late (team decision 2026-10-11).** Build everything first (Stages 1–2). Then write the missing tests and run the A+B+C re-test (Stage 3). Then do the experiment runs (Stage 5).
- One consequence: removing `InstallPQAuth` (A-F3, B-F1b, C-F9) still waits for the re-test, as in the dev plans, so it moves later too.
- Task ids are the dev plans' ids; **(new)** marks an id added here.
- When a PR finishes a task, change ☐ to ✅ and add the PR number (rule 6).

### 0.1 Done (on `main`)

| Track | Done |
|---|---|
| **A** (Bhargavi) | ✅ A-P1, A-P2, A-P4, A-P5: boot hook, `pq_verified`, station-made keys wired, key redaction (PR #34, #36) · ✅ A-F1: boot check in `--mode hybrid`, 1008 on failure (PR #42) · ✅ A-F2: `target_mode=hybrid` (PR #42) · ✅ A-F5: `/api/migration/rotate` (PR #42) · ✅ A-F4: dashboard API (`/api/events`, inspector fields, `/api/health` fields, `/dashboard/`) (PR #43) · ✅ A-F7: E5 attack steps + `experiments/e5_check.py` (PR #44) · ✅ L15, L16 → Accepted; L14, L28, L36 closed |
| **B** (Amal) | ✅ B-F5: E4 ML-DSA certificate sizes + `evidence/e4/e4_sizes.json` (L43) · ✅ B-F10: the Pi sends its INA219 reading as OCPP customData (L37, agent half) · ✅ B-P1: enrolment by public key (PR #32) · ✅ B-P2: boot verifier (PR #37) · ✅ B-F1: hybrid target · ✅ B-F3: AKI/SKI + `--server-san` · ✅ B-F4: halt profile · ✅ B-F8: `GPIOPower` code, `scripts/pi_charger.sh`, demo profile (Pi = CP0000) (all PR #38) · ✅ B-F2: key rotation with an overlap window (PR #39) · ✅ L06, L26, L35 closed; L08, L17, L27, L29 → Accepted |
| **C** (Koshambi) | ✅ C-P1–C-P6 · ✅ S1 at 50 / 250 / 500 (PR #35) · ✅ C-F6: live dashboard (PR #40; aligned with A-F4 in PR #46) · ✅ C-F5 part 1: E4 Contract 7 rows, E3 halt + rotation, headlines (PR #41) · ✅ C-F5 E5 part: the victim is counted, verdicts match `e5_check` (PR #46) · ✅ C-F7: `watch_migration` shows rotation (PR #46) · ✅ L10 closed; L12, L19, L30, L33 → Accepted |

### 0.2 Demo scenes — what is ready, what is missing

| # | Scene (FINAL 2-DAY PLAN storyline) | Code ready? | Still needed (step numbers below) |
|---|---|---|---|
| 1 | Fleet up over mutual TLS 1.3, Pi tile, security inspector | ✅ | Pi on the LAN (6, 7, 10) |
| 2 | Migration to hybrid while charging, canary → waves | ✅ | Live check (9) |
| 3 | Halt: refusers mid-fleet, wave rolled back | ✅ | Halt run (13); shown once on the results page (3, L38) |
| 4 | Reconnect storm: every charger re-proves its key at boot | ✅ | Live check (9) |
| 5 | Attack: stolen certificate → 1008; wrong identity refused (victim CP0045) | ✅ | Live check (9), rehearsal (17) |
| 6 | Curtailment: Pi LED dims, power chart drops, mW shown | ✅ (mW number: no) | Pi (7, 10); mW number (2, L37) |
| 7 | Key rotation without disconnection | ✅ | Live rotation (9, L40) |
| 8 | Results page: E1–E5 + S1 | ✅ (E4 certificate rows: no) | E4 (1, 14), runs (13), rebuild (15) |

**"Different modes":** the server runs one mode at a time. Live, the panel sees chargers before (classical identity only) and after migration (hybrid: an ML-DSA check at every boot) on one hybrid server. **Classical vs hybrid** timings are compared on the results page (E1, E2).

### Stage 1 — build (code only; the three tracks in parallel)

**1. B-F5 — E4 certificate sizes** ✅ (Track B PR, 2026-10-12) · Amal (B)
- **What:** write `experiments/e4_cert_sizes.py` and `crypto/pq_x509.py`. The results page can then show how big classical and ML-DSA certificate chains are, next to OCPP's 5,500 / 10,000-byte limits.
- **Needs first:** nothing. Decide the `cryptography` pin (L43).
- **Unblocks:** step 14 and the E4 rows in step 15. Track C's page already calls `available()` / `size_rows()`, so Track C needs no change.

**2. L37 — the Pi's real milliwatts on the dashboard: B-F10 (new) ✅ + A-F10 (new) ☐** · Amal (B) + Bhargavi (A)
- **What:** the Pi sends its INA219 milliwatts in each meter reading, and the server stores them as `measured_mw` on the station row. The Pi tile then shows, for example, "measured: 23.4 mW".
- **Needs first:** A and B agree the one-line format (see L37); B-F8 ✅.
- **Order:** agree the format, then B-F10 (agent side) and A-F10 (server metering + `StationView.measured_mw`) in parallel. If the agent change falls outside the files Track B may edit, send the diff to Track C.
- **Unblocks:** the number in scene 6. The dashboard is already ready for it. **Optional:** if it is skipped, show the Pi's console during scene 6.

**3. C-F5 part 2 — one run for a fleet started in two parts (L38)** ☐ · Koshambi (C)
- **What:** when one run is started as two load generators (the halt run, the demo), the analysis joins the two diaries into one run. The halt then appears once, with N = 50.
- **Needs first:** nothing. Built against made-up diaries; checked on the real halt evidence in step 15.
- **Unblocks:** step 15.

**4. A-F8 (draft) — demo runbook** ☐ · Bhargavi (A)
- **What:** one page with the exact commands per scene: windows, ports, fleet profiles, the `--db` + `certs\pq` reset, the Pi command and the expected screen. Plus a 3-line fix for each likely failure. Reuse `experiments/e5_attack.md` (scene 5) and `dashboard/README.md`.
- **Needs first:** nothing to start. It is finalised after steps 9 and 17.
- **Unblocks:** step 17.

**5. L34 — Track C's yes or no** ☐ · Koshambi (C), a decision only
- **What:** L34 (a passed migration key check also marks that connection verified) is waiting only for Track C's answer.
- **Needs first:** nothing.
- **Unblocks:** removing L34; one sentence for Contract 7 §7.7.

### Stage 2 — demo set-up (hardware and network, team together)

**6. A-F6 — the server on the LAN (Koshambi's laptop is the demo server)** ☐ · Bhargavi (A), with Koshambi
- **What:** use a phone hotspot; add a Windows firewall rule for port 9000; run the server with `--host 0.0.0.0 --tls`.
- **Certificates:** make new ones that include the hotspot IP: `python -m experiments.bootstrap_pki --count 50 --also CP0000 --server-san <laptop-ip>`. New certificates mean: restart the server, archive `certs\pq` and use a new `--db`.
- **Needs first:** B-F3 ✅; the hotspot IP.
- **Unblocks:** step 7.

**7. B-F9 — Pi bring-up** ☐ (software prepared: `scripts/pi_setup.sh` does every install step below, incl. the F8 folder trap, and `python -m agent.gpio_power` checks LED + INA219 without a server) · Amal (B); the wiring is done by the team
- **What:** install Raspberry Pi OS, clone the repo, create a venv and install `requirements.txt`. Install quantcrypt and compile it (**without** `--with-opt`), then run the 1312 check. Copy `certs/CP0000.*` and `root.pem` to the Pi. Run `bash scripts/pi_charger.sh <laptop-ip>`.
- **Needs first:** step 6; LED + INA219 wired; `gpiozero` and the INA219 library installed on the Pi (they are not in `requirements.txt`).
- **Unblocks:** step 10; scenes 1 and 6. Closes L13.

### Stage 3 — tests and integration (moved here, after building)

**8. T-1 (new) — the missing happy-path tests** ☐ · each track, for its own code
- **What:** one "it works" test per feature that was built without one:
  - Track B: B-F5;
  - Tracks B and A: B-F10 / A-F10;
  - Track C: step 3, plus PR #46 (E5 victim counting, `--mock-pi-mw`, the rotation view in `watch_migration`).
  - Then run the full suite on `main`.
- **Needs first:** Stage 1.
- **Unblocks:** step 9.

**9. T-2 = C-F1 — the A+B+C re-test, live** ☐ · Koshambi runs it; A and B attend
- **What:** on the demo laptop: hybrid server over TLS, fresh `--db`, archived `certs\pq`. Keep the dashboard and `watch_migration` open throughout.
  1. N = 3: migrate, restart the agents (boot checks pass), delete one key file (that charger is closed with 1008 every time).
  2. N = 50 with the stage 6 profile.
  3. Rotation at N = 10.
  4. The E5 small run (`experiments/e5_attack.md`, victim CP0003).
- **Needs first:** step 8; A-F1, A-F2, A-F4, A-F5, B-F1, B-F2 ✅.
- **Pass:**
  - the counts match;
  - the E5 verdicts from `analysis` are the same as `python -m experiments.e5_check` on the same log;
  - the rotation counts match the `rotation_*` lines.
- **Closes:** L39; L40 (live part). It is the remaining step of L01 and L02.
- **Unblocks:** Stage 4 and step 13.

**10. T-3 (new) — the Pi in the live fleet** ☐ · team
- **What:** the Pi joins over TLS. Its tile is visible. It migrates in the canary (CP0000 sorts first) and passes its boot checks. The LED dims when the limit slider is moved.
- **Needs first:** steps 7 and 9.
- **Unblocks:** step 17.

### Stage 4 — remove the old key flow (after the re-test)

**11. A-F3 ☐ + B-F1b ✅ built (Track B PR; merge only after step 9 passes) — the server and orchestrator stop using `InstallPQAuth`** · Bhargavi (A) and Amal (B). **B-F1b needs no merge order with A-F3:** the orchestrator still *accepts and ignores* `keypair_factory` / `install_message_factory`, so today's `csms/migration.py` keeps working until A-F3 drops them.
- **What:** delete the `keypair_factory` / `install_message_factory` path from `csms/migration.py` and `idmanager/orchestrator.py`, and make the new pair required.
- **Also update their own users of the old builder:** Track A's `tests/csms/test_log_redaction.py` imports `build_install_message`; Track B's `experiments/rotation_harness.py` mentions it.
- **Needs first:** step 9 passed.
- **Closes:** L04 (together with step 12) and the last part of L01.
- **Unblocks:** step 12.

**12. C-F9 (new) — the charger drops `InstallPQAuth`** ☐ · Koshambi (C)
- **What:** delete the agent's old handler and builder (`agent/client.py`, `agent/station.py`, `agent/pqc_messages.py`, `agent/pq_identity.py`). Fix Track C's tests, `harness/load_generator.py` and the E4 wording that still mention it.
- **Needs first:** step 11 merged, because their tests import the builder.
- **Closes:** L42. The full suite must pass.

### Stage 5 — experiment runs and the results page

**13. C-F4 — experiment runs (reduced matrix)** ☐ · Koshambi; A and B present for the halt and E5
- **Settings:** TLS on, `--watch-fleet`, `--watch-every 2`; runs end on their own (the `--charge-for` rule).
- **Runs:**
  - E1 and E2: classical vs hybrid at N = 50 and 100.
  - E3: N = 100 clean; N = 50 halt (Track A's `fake_station` CP0021–25); N = 50 rotation.
  - E5: the small run.
- **Evidence:** only `*.jsonl` and `*.json`, into `evidence/2026-10-12_final/`, then a PR.
- **Needs first:** Stage 4 (the runs use the final code); step 9.
- **Closes:** L09, L11, L40.
- **Unblocks:** step 15.

**14. B-F5 run — the E4 numbers** ✅ (same PR as step 1) · Amal (B)
- **What:** run `experiments/e4_cert_sizes.py`; it writes `evidence/e4/e4_sizes.json`.
- **Needs first:** step 1.
- **Closes:** L43.
- **Unblocks:** step 15.

**15. C-F5 final — rebuild the results page** ☐ · Koshambi (C)
- **What:** `python -m analysis.run --logs evidence\2026-10-12_final`. Check each run's trust status before quoting numbers, then write one headline line per experiment for the slides.
- **Needs first:** steps 3, 13 and 14.
- **Closes:** L38.

### Stage 6 — finish

**16. Register clean-up (A-F9, B-F7, C-F8)** ☐ · all
- **What:** remove every L item that Stages 3–5 closed: L01, L02, L04, L09, L11, L13, L32 + L37, L38–L40, L42, L43.
  - Remove L05 once all three tracks say "on Python 3.13".
  - Add the L31 and L34 sentences to Contract 7.
- **Needs first:** Stages 3–5.

**17. Dress rehearsal and freeze (A-F8, CP4)** ☐ · all; Bhargavi leads
- **What:** run scenes 1–8 twice on the demo laptop + Pi, following the runbook. Fix only what breaks. Freeze `main` and tag `v1.0`.
- **Needs first:** everything above.

---

## 1. Blocking E1 / E2 (all decided; built through Contract 7)

| No. | Finding | Status | Action | Owner |
|---|---|---|---|---|
| **L01** | **The server makes every charger's private key and sends it over the network.** The charger keeps it in memory only, so a restart loses it. *(old: F2+F3, A-F13, B-F13)* | **Agreed (A, B, C)** | The charger generates its own ML-DSA key, sends **only the public key**, and saves the private key in `certs/pq/<id>.json` (ignored by git). **Specified in Contract 7 §7.2–7.4.** **Progress 2026-10-09:** Track C's side merged (C-P1, C-P2). Track B's side done (B-P1): the orchestrator takes `enrolment_request_factory` + `public_key_parser`; live check with Track C's builders and agents: 3 migrated, 1 incompatible, key files only on the chargers, no `private_key` in the server log or diary. ~~**Remaining:** Track A switches `csms/migration.py` to the new options (A-P4)~~ **Track A side done (A-P4, Track A PR 2, 2026-10-10):** `csms/migration.py` passes `enrolment_request_factory=build_enrolment_request` and `public_key_parser=lambda r: parse_enrolment_reply(r.data)`; live check with 3 real agents in `--mode hybrid`: 3 migrated, 1 incompatible, matching `key_id` on every `pq_enrolled`/`pq_auth` line, server log shows 3 `RequestPQEnrolment`, **0** `InstallPQAuth`, **0** `private_key`; Track A's refuser fixture still rolls its wave back. **Remaining:** the A+B+C re-test (C-P8); then `InstallPQAuth` is removed (A-P6, B-P5). | B (orchestrator install step), C (`agent/pqc_messages.py`, `agent/pq_identity.py`); A reviews |
| **L02** | **The security mode is only a label.** The key is checked once, at migration, so "classical" and "post-quantum" chargers connect in exactly the same way. E1/E2 would compare identical things. *(old: F1, A-8)* | **Agreed (A, B, C)** | The server challenges every migrated charger after **each** accepted boot (Track A's boot listener); a failed check **closes the connection** (code 1008). **Specified in Contract 7 §7.5–7.7.** **E2 "recovered" in post-quantum mode = connected + boot accepted + key check passed.** **Presentation:** the migration must be visibly obvious to the panel (dashboard migration timeline, live ticker of waves and key checks, a readable console view). **Progress 2026-10-10 (Track A PR 2):** A-P1 boot hook (`registry.add_boot_listener`, fired from `@after("BootNotification")` on its own task) and A-P2 (`StationView.pq_verified` + `pq_check_required`, per-mode `is_recovered`) are built and tested. ~~**Remaining:** B-P2 boot verifier~~ **Track B side done (B-P2, 2026-10-10):** `idmanager/boot_verifier.py`, `BootVerifier(...).verify_on_boot(station_id) -> bool`: not enrolled → True with no check; enrolled → challenge naming the enrolled `key_id`, verify, one `pq_auth` line with `trigger: "boot"` (`source` added by Track A's emitter, L31); every failure → False, never raises. Live check with A-P3 wired in a scratch copy only (`--mode hybrid`, 3 real agents): migrate 3/1/0/0 → reconnect: 3 boot checks pass, all `pq_verified` → delete CP0002's key file → its boot check fails, `connection_closed` `pq_auth_failed`, every reconnect refused again. **Remaining:** A-P3 (`--mode hybrid` registers it; close 1008 on failure; see L36), then the A+B+C re-test (C-P8). **Track A side done (A-F1, PR 3, 2026-10-10):** `--mode hybrid` registers Track B's boot verifier; pass → that connection `pq_verified`; fail → closed 1008 with one `connection_closed` line (`reason: "pq_auth_failed"`); hybrid without migration refuses to start. Live over mutual TLS: 3 migrated → rotated → reconnected → 2 boot checks passed with the rotated keys; the charger whose key file was deleted was refused on all 5 reconnects. **Remaining:** the A+B+C re-test (C-F1). | A (boot hook, recovered rule), B (challenge on boot), C (agent; display) |

## 2. Agreed fixes, not yet done

| No. | Finding | Status | Action | Owner |
|---|---|---|---|---|
| L04 | **Private keys appear in the server log.** The `ocpp` library logs every `InstallPQAuth` message at INFO, key included. GitGuardian flagged the copies once committed in `evidence/`. *(old: A-F14)* | **Agreed (A, B, C)** | 1. Track A adds a log filter that blanks `private_key` values. 2. Never commit `*.log`. 3. Koshambi closes the GitGuardian incident as a test credential. 4. No git-history rewrite (throwaway test keys). **Root fix: Contract 7 removes `InstallPQAuth` (§7.8).** **Step 1 done (Track A, 2026-10-10):** `csms/log_redaction.py` blanks every `private_key` value in the server's output log, including the escaped form inside `DataTransfer.data` and tracebacks; checked live (3 `InstallPQAuth` sends logged, 3 values `[REDACTED]`). Item stays open until `InstallPQAuth` is removed (A-P6 / B-P5). | A (filter ✔), C (incident) |
| L05 | **Coarse timers on Windows before Python 3.13.** `time.monotonic()` moves in ~15.6 ms steps. The agent is fixed (PR #26); Track A's timers (`monotonic_ns`: server handshake time, key-check round trip) are not. | **Agreed (A, B, C)** | **All tracks move to Python 3.13+.** Remove this item once all three confirm they are on 3.13. **Track A: on 3.13 (Mac, 3.13.16, 2026-10-10; full suite passes).** | A, B, C |
| L09 | **E3 showed the rollback but not the halt.** The failing chargers were in the last wave. | Agreed | One extra E3 run with the refusers mid-fleet (for example CP0021–25): a new fleet profile, plus the load generator run twice with `--start-index`. **Profile ready (B-F4, 2026-10-10):** `tests/fixtures/fleet_profile_halt_n50.json` (refusers CP0021–25; expected phase `rolled_back`: migrated 15, rolled_back 10, pending 25 — checked against the real orchestrator). | C runs; B (profile contents), A (fixture) |
| L11 | **Session runs were stopped with Ctrl-C,** so they are marked "incomplete". | Agreed | Every run ends on its own. `--charge-for` follows the rule in Track C's plan (ramp + action + 30 s observation + 30 s margin). | C |

## 3. Planned (in the owner's plan)

| No. | Finding | Status | Action | Owner |
|---|---|---|---|---|

## 4. Not decided

| No. | Finding | Status | Action | Owner |
|---|---|---|---|---|
| L13 | **Raspberry Pi charger node: setup checks still open.** Hardware is **agreed and bought (2026-10-09):** Raspberry Pi + INA219 sensor + an LED with a 330 Ω resistor on **GPIO18 (PWM)**, no relay. The LED stands for the charging power: on/off = contactor closed/open, brightness (PWM duty) = power limit. | Agreed (hardware); checks open | On the Pi: Track A's quantcrypt 1312 check; certificate SAN for the laptop's `.local` name (old B-R4). Track C builds `GPIOPower` (C9) behind Contract 5. How the INA219 reading becomes an OCPP meter value: **L32**. **Update 2026-10-11 (Track C, from `main`):** the Pi software moved to Track B and is built (B-F8: `agent/gpio_power.py`, `--power gpio`, `scripts/pi_charger.sh`); the Pi's id is **CP0000** (in `fleet_profile_demo.json`); certificates with the hotspot IP come from `bootstrap_pki --also CP0000 --server-san <ip>` (B-F3). **Open:** the bring-up on the Pi itself (B-F9: 1312 check, certificates, `gpiozero` + INA219 library, which are not in `requirements.txt`) and the server on the LAN (A-F6). Tracker §0, steps 6, 7, 10. | B (B-F9); A (A-F6) |

## 4b. New items (2026-10-08)

| No. | Finding | Status | Action | Owner |
|---|---|---|---|---|

## 4c. New items (2026-10-09)

| No. | Finding | Status | Action | Owner |
|---|---|---|---|---|
| **L31** | **Where `source` in the post-quantum events comes from.** Contract 7 §7.6 lists `source: "orchestrator"\|"boot_verifier"` in the `pq_auth` payload. In code, Track B's orchestrator must **not** put `source` in a payload: Track A's `orchestrator_emitter` (`csms/migration.py`) already adds `source="orchestrator"`, and a second `source` keyword raises `TypeError` inside `EventLog.emit` (verified 2026-10-09). B-P1 follows this, and the live diary shows `source: "orchestrator"` on every `pq_enrolled` and `pq_auth` line. **Open for B-P2:** the boot verifier's `source: "boot_verifier"` must come the same way, from the emitter Track A builds for it (A-P3). | **Agreed (A, B)** | Track A's emitter for the boot verifier writes `source="boot_verifier"`; Track B's verifier never puts `source` in its payload. Contract 7 text unchanged (the event shape is the same); add one sentence to §7.6 at the next revision. **Approved by Track A (2026-10-10).** Track A PR 2: `orchestrator_emitter(event_log, source=...)` is the one emitter for both (A-P3 builds it with `source="boot_verifier"`), and a `source` passed by a caller is dropped with a warning instead of raising `TypeError` and losing the event. Track C informed. | A + B |
| **L32** | **The Pi's power reading is milliwatts, not kilowatts.** The INA219 measures the LED circuit (tens of mW), but OCPP meter values, the fleet power line and E5's curtailment chart expect a believable charger (up to 7.4 kW). Two options: **(a)** report a **scaled** value, e.g. PWM duty × 7.4 kW, labelled "scaled" (the INA219 then proves the LED really changed); **(b)** report the **real** milliwatts. With (b) the Pi's power is invisible next to 499 simulated 7.4 kW chargers; with (a) the number is not a physical measurement. | **Decided: (a) scaled** (FINAL 2-DAY PLAN, 2026-10-10) | *Recommended:* (a), with the raw INA219 mW reported alongside so the scaling is visible and checkable. Either way the Pi's energy is labelled (as for E6 nodes, `energy_source: "charger-reported"`) and never presented as a measured 7.4 kW. Decide before C9. **Built (B-F8):** `agent/gpio_power.py` reports duty × 7.4 kW; the INA219 mW is logged on the Pi's console every 5 s (`raw_reading()`). Showing it on the dashboard tile needs a small Track A + C step (open). | Team; C (C9) |

## 4d. New items (2026-10-10, Track A)

| No. | Finding | Status | Action | Owner |
|---|---|---|---|---|
| **L34** | **A charger verified by its migration check counts as recovered (gap in Contract 7 §7.5/§7.7).** §7.7 sets `pq_verified` only from the **boot** check, so in `--mode hybrid` every charger migrated on its current connection would become enrolled (`pq_check_required`) but unverified, and show as **not recovered** until it reconnected. | Proposed (Track A; needs B and C) | **Built in Track A PR 2:** a **passed** migration key check (`pq_auth`, `result: success`) also marks that charger's current connection `pq_verified` (`orchestrator_emitter(on_pq_auth_success=registry.mark_pq_verified)`). Same evidence as a boot check: a fresh nonce signed with the enrolled key, on the same connection. Live: 3 migrated in hybrid, all 3 stayed recovered. Add one sentence to §7.7 at the next contract revision. Remove when B and C agree. **Track B agrees (2026-10-10):** the same evidence as a boot check. | A (B ✔, C agree) |

## 4e. New items (2026-10-10, Track B)

| No. | Finding | Status | Action | Owner |
|---|---|---|---|---|

## 4f. New items (2026-10-10, Track C)

| No. | Finding | Status | Action | Owner |
|---|---|---|---|---|
| **L37** | **The Pi's measured milliwatts never reach the server.** `GPIOPower` (B-F8) reads the INA219 and logs the raw mW on the Pi's own console only; nothing sends it to the CSMS, so `StationView` has no `measured_mw` and the dashboard's Pi tile shows "measured: on the Pi's console" instead of the number (FINAL 2-DAY PLAN, scene 6 and C-F6). | **Decided 2026-10-12 (B, with Track A to confirm): customData** | *Recommended (smallest):* the Pi agent adds one extra sampled value to each meter reading (measurand `Power.Active.Import`, `context: "Other"`, unit `mW`, or a `customData` field); Track A's metering stores it as `measured_mw` on the station view. The dashboard already shows `measured_mw` whenever it is present. **Until then:** show the Pi's console next to the dashboard during scene 6. **Update 2026-10-11 (Track C):** tracker §0 step 2 names the two halves B-F10 (agent) and A-F10 (server). If the meter-value change falls outside the agent files Track B may edit (`agent/gpio_power.py`, `agent/config.py` `--power`, the backend choice in `agent/station.py`), send Track C the diff. The dashboard mock now sends `measured_mw` / `power_source` **only with `--mock-pi-mw`**, because Track A's test `test_real_rows_and_health_have_every_field_the_mock_has` checks that the default mock has no field the real server lacks; once A-F10 adds `measured_mw` to `StationView`, the mock default can send it again. **Decision (Track B, B-F10, 2026-10-12):** the reading travels as OCPP **customData on the existing power sample** — `{"vendorId": "pqcharge.hw", "measuredMw": 23.4, "measuredMa", "measuredV", "ledDuty", "powerBasis": "scaled", "sensor": "INA219"}` — **not** as a second `Power.Active.Import` sample in mW: `csms/metering.py` takes the LAST power sample and treats the unknown unit `mW` as watts, so a second sample would overwrite the Pi's power with the milliwatt number and log `unrecognised_meter_value` every 10 s (checked in the code). With customData the power value is unchanged and today's server ignores the extra data (checked live: no error, power 7,400 W as before). **Built (agent side):** `GPIOPower.meter_custom_data()`, `msg.meter_value(..., power_custom_data=)`, passed through `client.send_transaction_event` and the three live calls in `agent/station.py` (simulated chargers send nothing new). **Remaining — A-F10 (Track A):** `parse_meter_values` reads `sample["custom_data"]["measured_mw"]` of the power sample into `MeterReading.measured_mw`; `record_meter(..., measured_mw=)` stores it; `StationView.measured_mw`. Checked in a scratch copy: `/api/fleet/CP0000` → `measured_mw` 46.8, then 23.4 after a 3,700 W limit. |

## 4g. New items (2026-10-11, Track C)

| No. | Finding | Status | Action | Owner |
|---|---|---|---|---|
| **L38** | **A fleet started as two load generators shows up as two runs on the results page.** The halt run (`--n 20` + `--n 25 --start-index 26`, profile `fleet_profile_halt_n50.json`) and the demo fleet (`--n 44` + `--n 4 --start-index 46`) each write two tester diaries. The analysis makes one run per diary, and migration lines are fleet-wide, so the same halt appears twice: as an E3 run with N = 20 and another with N = 25, each showing the whole migration. | Planned | Track C, C-F5 part 2 (tracker step 3): join the diaries of one run (same experiment, overlapping time, same server log) into one run, with N = their sum. Check it on the real halt evidence (step 15). **Until then:** quote the halt from one of the two runs only. | C |
| **L39** | **Track C's PR #46 has been checked only against the dashboard mock and Track A's API tests, not against a live server.** Not yet run on a real hybrid server: the E5 victim counting, the `boot_check` chip, the CP0000 Pi tile and the rotation view in `watch_migration`. | Planned | The A+B+C re-test (C-F1, tracker step 9): dashboard and `watch_migration` open while migrating, rotating and running the E5 small run. The E5 verdicts from `analysis` must equal `python -m experiments.e5_check` on the same log. Remove when that passes. | C (A, B attend) |
| **L40** | **The rotation counts are read as Track B describes them; no live rotation has been watched yet.** The dashboard, `watch_migration` and E3 read a rotation's `MigrationStatus` as `migrated` = rotated and `rolled_back` = kept its old key (`idmanager/api.py`, `kind: "rotation"`). | Planned | Check during rotation at N = 10 (tracker step 9) and in the E3 rotation run (step 13): `/api/migration` counts must equal the `rotation_completed` / `rotation_failed` lines. | C (B confirms) |
| **L42** | **The charger still understands `InstallPQAuth`, and no dev-plan task removes it.** A-F3 and B-F1b remove the server and orchestrator side after the re-test. Track C's agent keeps the old handler and builder (`agent/client.py`, `agent/station.py`, `agent/pqc_messages.py` `build_install_message`, `agent/pq_identity.py`). Track C's tests, `harness/load_generator.py` and `analysis/measures/e4_sizes.py` still mention it. Other tracks use the builder too: Track A's `tests/csms/test_log_redaction.py` imports it, and Track B's `experiments/rotation_harness.py` mentions it. Harmless once the server never sends it, but two flows stay in the code. | Proposed (needs A, B to agree the order) | New task **C-F9** (tracker step 12), right after A-F3 + B-F1b (step 11): delete the agent's handler and builder, and update Track C's tests. A and B first remove their own uses of `build_install_message`, so no PR breaks another track's tests. `csms/log_redaction.py` stays or goes by Track A's choice. **Update 2026-10-12 (Track B, B-F1b):** the orchestrator side is removed; `experiments/rotation_harness.py` and Track B's `tests/idmanager/test_orchestrator.py` now use the Contract 7 flow. **Track C's `tests/analysis/test_seams_real_code.py` built the orchestrator with the old flow**; the B-F1b PR moves it to `enrolment_request_factory` + `public_key_parser` (2 lines, all its assertions unchanged and passing) — **needs Koshambi's OK**. Left for A-F3: `csms/migration.py` arguments and `tests/csms/test_log_redaction.py`; for C-F9: the agent side. |

## 5. Accepted limitations (stay in the report)

| No. | Limitation | Why it is accepted |
|---|---|---|
| L20 | **Post-quantum protection is at the application layer only (Option B).** TLS stays classical X.509, so the OCPP traffic itself is not post-quantum encrypted. | Post-quantum TLS cannot be loaded into Windows CPython (docs R3), and the installed `cryptography` cannot make ML-DSA certificates. Keeps third-party chargers (E6) working. |
| L21 | **Write-behind persistence:** a hard kill of the server loses up to one flush interval (default 2 s) of station state and of enrolled keys. *(A-F4)* | A trade-off for speed at fleet scale. Measured and stated. |
| L22 | **Every `/api` endpoint is GET,** including ones that change things. *(A-F7)* | The `websockets` HTTP parser rejects other methods. Mitigation: state-changing GETs sit behind mutual TLS. |
| L23 | **quantcrypt 1.0.0 can install without its engine** (seen on an Intel Mac with Python 3.11). *(A-F8)* | The server probes key generation at startup and disables migration with a reason instead of crashing. Run the 1312 check on every machine. |
| L24 | **The SAP simulator's energy numbers are not physical,** and it does not implement SetChargingProfile in 2.0.1 mode. *(A-F10, A-F11)* | It is used only for E6 interoperability; its energy never enters an energy result. Third-party curtailment is unproven (EVerest optional). |
| L25 | **The agent's offline queue holds at most 2,000 events;** older ones are dropped when full. | Bounded memory at 500 chargers; drops are counted and reported by C6. |
| L12 | **E6's second half was not done:** our charger against a third-party CSMS (CitrineOS / MaEVe). *(Accepted 2026-10-10, FINAL 2-DAY PLAN.)* | The first half passed (a third-party OCPP 2.0.1 charger completed sessions on our server, 0 protocol errors), so interoperability is shown in one direction. Future work. |
| L19 | **Charger key files cannot be locked down on Windows** (no `chmod 0600`). *(Accepted 2026-10-10.)* | Simulated chargers only; the Raspberry Pi (Linux) writes its key file with mode 0600. |
| L30 | **The charger cannot tell a boot key check from any other challenge;** the analysis counts the *first* challenge of a connection opened with a key held as the boot check (E1 secure-ready). *(Accepted 2026-10-10.)* | In every measured run the boot check is the first challenge on such a connection; a hybrid run with keys but no boot checks is flagged (`hybrid_without_boot_checks`). Rotation runs are analysed separately. Future work: a `reason` field in `PQAuthChallenge` (Contract 7 v2). |
| L33 | **Tester diaries get large at fleet scale** (98 % `fleet_snapshot` lines; 46 MB at 500 chargers). *(Accepted 2026-10-10.)* | Final runs stay at N ≤ 100 with `--watch-every 2` (a few MB each); S1's 500-charger evidence is committed as is (every file < 50 MB). Future work: gzip-compressed diaries. |
| L17 | **Pure PQC (post-quantum TLS) is not built.** Windows CPython's `ssl` (OpenSSL 3.0) cannot do ML-KEM/ML-DSA TLS; the project measures classical vs **hybrid** (Contract 7). | FINAL 2-DAY PLAN (2026-10-10): cut; stated as future work. |
| L29 | **No pure-PQC migration flow** (switching chargers to post-quantum TLS certificates in waves). | Depends on L17; future work. |
| L27 | **A server restart in the middle of a migration is untested.** Per-charger state and enrolled keys survive; the running migration task does not. | Lab project; not part of the demo. |
| L08 | **No operator certificate:** under TLS the fleet watcher uses a station's certificate for `/api`, and the identity check does not cover `/api` (verified 2026-10-09). | Lab set-up on a private hotspot; `/api` still requires a CA-signed client certificate. |
| L15 | **The event log fsyncs every event;** its effect on server timing at fleet scale was not measured. *(Accepted 2026-10-10, FINAL 2-DAY PLAN.)* | E1/E2 run only at N ≤ 100, where S1 showed the CSMS far from saturation (CPU p95 ≤ 45 % of one core at 500 chargers). |
| L16 | **E6 (a third-party charger on our server) was shown over `ws://` only, not `wss://`.** *(Accepted 2026-10-10, FINAL 2-DAY PLAN.)* | Our own chargers run over mutual TLS 1.3 in every TLS run; E6's point is protocol interoperability, which passed. |
| L41 | **`watch_migration` (the console backup view) can show a rotated charger as "old key" after it reboots.** Its `N` (new key) letter comes from the charger's last key check (`last_pq_check`, trigger `rotation`). A later boot check replaces that. The fallback (the key changed since the view started) only works if the view was already open when the rotation began. *(Accepted 2026-10-11, Track C.)* | It is only the console backup. The dashboard ticker and the results page (E3 rotation: old → new `key_id` per charger) are the record. In the demo, open `watch_migration` before pressing "Rotate keys". |

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
| L26 (key rotation had no overlap window) | Track B B-F2 (2026-10-10): `MigrationOrchestrator.start_rotation(wave_size, canary_count)`. The new key is held NEXT TO the old one (`PQAuthenticator.stage_key`), proven with a challenge naming the new `key_id`, and only then replaces it (`commit_staged`); on failure the station keeps its old key; a wave over the threshold puts every old key of that wave back. Events `rotation_started` / `rotation_completed` / `rotation_failed`; `pq_auth` with `trigger: "rotation"`; run-level events carry `kind: "rotation"`. Live (scratch endpoint): 3 rotated while charging, 0 sessions closed. **Track A:** A-F5 endpoint `/api/migration/rotate` + pass `key_id_for` in `build_migration`. **Track C:** analysis/dashboard read `kind` and the rotation events. |
| L06 (certificates lacked AKI/SKI) | Track B B-F3 (2026-10-10): `crypto/ca.py` adds AKI + SKI + BasicConstraints to every certificate and KeyUsage to the root; mutual TLS with Python 3.13's strict `ssl.create_default_context` passes. Regenerate certificates with `bootstrap_pki`. |
| L43 (E4's ML-DSA certificate rows missing on `main`) | Track B B-F5 (2026-10-12): `crypto/pq_x509.py` + `experiments/e4_cert_sizes.py` merged; **pin kept at `cryptography>=42`** — with `cryptography` 50+ the rows come from real ML-DSA certificates (verified against the root), otherwise they are computed from FIPS 204 sizes + measured X.509 overhead and labelled "(computed)" (agrees with the real sizes within 8 bytes, tested). `available()` is now always True, so Track C's page shows the rows everywhere. Evidence: `evidence/e4/e4_sizes.json`. |
| L35 (one outstanding challenge per station: a boot check and a migration check for the same charger cancelled each other, both failed) | Track B B-P2 (2026-10-10): `PQAuthenticator` keeps up to 8 outstanding challenges per station, each verified by its nonce (`verify_response(..., nonce=)`); callers without a nonce keep the old rule. Test: `test_overlapping_boot_and_migration_checks_both_pass` |
| L14 (Contract 3 lacked two event types) | Track A PR 1 (2026-10-10): `EventType.STATION_DEFERRED`, `EventType.MIGRATION_FAILED` (same strings as before) |
| L28 (migration target labelled `pqc`, not `hybrid`) | Track B B-F1 + Track A PR 3 (A-F2, 2026-10-10): `/api/migration/start` takes `target_mode=hybrid`; `pqc` is refused with "pure PQC not built (L17)" |
| L36 (a late boot-check verdict could act on a newer connection) | Track A PR 3 (A-F1, 2026-10-10): the connection that booted is fixed when the boot is announced (`registry.booted_session`); a late False/True never closes or marks a newer connection; the boot verifier's emitter has no `on_pq_auth_success`. Tested both ways |

**Note:** `docs/limitations.md` R4 ("certificates carry no SAN") is out of date: the SAN mechanism was merged (Track B R2). Track B should update it.