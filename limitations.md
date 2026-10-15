# PQCharge — Open Items and Limitations

**The single register of everything not yet resolved.** Started 2026-10-03, after the A+B+C integration. **Updated 2026-10-09:** fleet-migration audit added L26–L29. **Later, 2026-10-09 (Track B, B-P1):** L01 progress, L08 verified, L31 added. **2026-10-10 (Track B, B-P2):** L02 progress, Track B agrees to L34, L35 found and fixed, L36 added. **2026-10-10 (Track B, B-F1/F3/F4/F8):** L06 resolved; L28 B side done; L09 profile ready; L32 decided and built; L17, L29, L27, L08 moved to Accepted (FINAL 2-DAY PLAN). **B-F2:** L26 resolved (rotation with overlap window). **2026-10-11 (Track C):** work tracker added (§0); L38–L43 added; L13 and L37 updated. **2026-10-12 (Track B, B-F5 + B-F10):** L43 decided and closed; L37 decided (customData), Track B half built. **2026-10-12 (Track B, B-F1b + B-F9 prep):** orchestrator side of `InstallPQAuth` removed (merge after step 9); L42 updated; Pi set-up script. **2026-10-12 (Track B, step 16 part 1, after the step 9 re-test):** L02 and L32 closed; L01, L42, L13 updated; L20 wording corrected; steps 8 (Track B), 9 and 11 (B-F1b, PR #49) ticked; `docs/limitations.md` R4 corrected and Track B's closed items recorded there. **2026-10-12 (Track C, step 3 + L42):** C-F5 part 2 built (L38: a run started as two load generators is analysed as one); L34 agreed by A, B, C and closed; L37 closed (A-F10, PR #51, + the dashboard mock); `InstallPQAuth` removed from every track (A-F3 + C-F9, and the orchestrator's last compatibility keywords) — L42, L01 and L04 closed; steps 2, 3, 5, 11, 12 ticked. **2026-10-12 (Track C, for Track A — step 4 + register clean-up):** Stage 1 complete ✅; demo runbook v1.0 (`docs/Demo_Runbook.md`, A-F8) — step 4 ticked; L05 and L31 closed; L04 GitGuardian incident confirmed closed; L38–L40 notes updated (what is and is not checked yet). **2026-10-13 (Track C, for Track A):** step 6 (A-F6) ✅ laptop side — hotspot IP 172.20.10.6, firewall, server on the LAN; Stage 2 split into software (2S: new steps 6a–6d) and hardware (2H: steps 7, 10) so the Pi no longer blocks the software fleet, the experiment runs or the results page. **2026-10-14 (Track C, step 8):** Track C's tests added (16); `bootstrap_pki` now deletes the previous run's certificate files; 6b reopened (key / `--db` mismatch found in the `sw1` logs). **Later, 2026-10-14 (Track C, runbook v1.2 + dashboard redesign):** demo runbook v1.2 = variants V1–V8 (step 4); the dashboard redesigned as an animation; the server now logs refused TLS handshakes (Track A file, team OK); the Pi's LED blinks while it is migrated (Track B file, team OK); `experiments/forge_cert.py`; 16 more tests; L44–L48 added. **Later, 2026-10-14 (Track C, runbook v1.3 + results page by run):** `python -m analysis.run --run <name>` analyses one demo run (main and halt servers apart), keeps its diaries in `evidence/demos/<name>/` and REPLACES its result on a re-run; the results page picks a run (newest analysed first), "All runs combined" or "Experiments"; S1 removed from the page; runbook v1.3 (`$IP`, `$RUN`, analysis after V7); `agent/station.py` line endings fixed; L38 join fixed (a part started twice was chained); L48 corrected; L49, L50 added. **2026-10-13 (Track C, step 6c):** 6a, 6b ✅; 6c ✅ — 500-charger profile, dashboard dense grid, runbook v1.1. **Earlier, 2026-10-08:** every team item is agreed by A, B and C. **Contract 7 is frozen** (approved by A, B, C), so L03 (meaning of the modes) and L18 (contract approval) are resolved and removed.

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
- Removing `InstallPQAuth` (A-F3, B-F1b, C-F9) waited for the re-test, as in the dev plans. The re-test passed on 2026-10-12 (step 9), and the removal was finished the same day (steps 11–12).
- **Hardware does not block software (2026-10-13):** Stage 2 is split into 2S (software, real server, no Pi) and 2H (the Pi). Only steps 10 and 17 need the Pi.
- Task ids are the dev plans' ids; **(new)** marks an id added here.
- When a PR finishes a task, change ☐ to ✅ and add the PR number (rule 6).

### 0.1 Done (on `main`)

| Track | Done |
|---|---|
| **A** (Bhargavi) | ✅ A-P1, A-P2, A-P4, A-P5: boot hook, `pq_verified`, station-made keys wired, key redaction (PR #34, #36) · ✅ A-F1: boot check in `--mode hybrid`, 1008 on failure (PR #42) · ✅ A-F2: `target_mode=hybrid` (PR #42) · ✅ A-F5: `/api/migration/rotate` (PR #42) · ✅ A-F4: dashboard API (`/api/events`, inspector fields, `/api/health` fields, `/dashboard/`) (PR #43) · ✅ A-F7: E5 attack steps + `experiments/e5_check.py` (PR #44) · ✅ A-F10: `measured_mw` / `power_source` on the station row (PR #51) · ✅ A-F3: the server wiring no longer passes the `InstallPQAuth` factories (done in Track C's L42 PR #52, by team decision) · ✅ A-F8 v1.0: demo runbook `docs/Demo_Runbook.md` (written by Track C for Track A, 2026-10-12) · ✅ L15, L16 → Accepted; L14, L28, L36 closed |
| **B** (Amal) | ✅ B-F1b: orchestrator uses Contract 7 only; Pi set-up script + `python -m agent.gpio_power` self-test (PR #49) · ✅ B-F5: E4 ML-DSA certificate sizes + `evidence/e4/e4_sizes.json` (L43) · ✅ B-F10: the Pi sends its INA219 reading as OCPP customData (L37, agent half) · ✅ B-P1: enrolment by public key (PR #32) · ✅ B-P2: boot verifier (PR #37) · ✅ B-F1: hybrid target · ✅ B-F3: AKI/SKI + `--server-san` · ✅ B-F4: halt profile · ✅ B-F8: `GPIOPower` code, `scripts/pi_charger.sh`, demo profile (Pi = CP0000) (all PR #38) · ✅ B-F2: key rotation with an overlap window (PR #39) · ✅ L06, L26, L35 closed; L08, L17, L27, L29 → Accepted |
| **C** (Koshambi) | ✅ C-P1–C-P6 · ✅ S1 at 50 / 250 / 500 (PR #35) · ✅ C-F6: live dashboard (PR #40; aligned with A-F4 in PR #46) · ✅ C-F5 part 1: E4 Contract 7 rows, E3 halt + rotation, headlines (PR #41) · ✅ C-F5 E5 part: the victim is counted, verdicts match `e5_check` (PR #46) · ✅ C-F7: `watch_migration` shows rotation (PR #46) · ✅ work tracker (PR #47) · ✅ C-F5 part 2: one run for a fleet started as two load generators (L38) · ✅ C-F9: the charger drops `InstallPQAuth` (L42) (C-F5 part 2 + C-F9: PR #52) · ✅ L34 agreed · ✅ demo runbook v1.0 for Track A (step 4) · ✅ L10 closed; L12, L19, L30, L33 → Accepted |

### 0.2 Demo scenes — what is ready, what is missing

| # | Scene (FINAL 2-DAY PLAN storyline) | Code ready? | Still needed (step numbers below) |
|---|---|---|---|
| 1 | Fleet up over mutual TLS 1.3, Pi tile, security inspector | ✅ | Software fleet live (6b); the Pi (7, 10) |
| 2 | Migration to hybrid while charging, canary → waves | ✅ | Live check (9) |
| 3 | Halt: refusers mid-fleet, wave rolled back | ✅ | Halt run (13); shown once on the results page: code ✅ (3), checked on the real evidence in 15 (L38) |
| 4 | Reconnect storm: every charger re-proves its key at boot | ✅ | Live check (9) |
| 5 | Attack: stolen certificate → 1008; wrong identity refused (victim CP0045) | ✅ | Live check (9), rehearsal (17) |
| 6 | Curtailment: Pi LED dims, power chart drops, mW shown | ✅ (mW number too: B-F10 + A-F10) | Fleet power part: 6b; LED and mW: the Pi (7, 10) |
| 7 | Key rotation without disconnection | ✅ | Live rotation (9, L40) |
| 8 | Results page: E1–E5 + S1 | ✅ (E4 certificate rows too: B-F5) | Runs (13), rebuild (15) |

**"Different modes":** the server runs one mode at a time. Live, the panel sees chargers before (classical identity only) and after migration (hybrid: an ML-DSA check at every boot) on one hybrid server. **Classical vs hybrid** timings are compared on the results page (E1, E2).

### Stage 1 — build (code only; the three tracks in parallel) ✅ (complete 2026-10-12: steps 1–5)

**1. B-F5 — E4 certificate sizes** ✅ (Track B PR, 2026-10-12) · Amal (B)
- **What:** write `experiments/e4_cert_sizes.py` and `crypto/pq_x509.py`. The results page can then show how big classical and ML-DSA certificate chains are, next to OCPP's 5,500 / 10,000-byte limits.
- **Needs first:** nothing. Decide the `cryptography` pin (L43).
- **Unblocks:** step 14 and the E4 rows in step 15. Track C's page already calls `available()` / `size_rows()`, so Track C needs no change.

**2. L37 — the Pi's real milliwatts on the dashboard: B-F10 (new) ✅ + A-F10 (new) ✅ (PR #51)** · Amal (B) + Bhargavi (A). Track C: the dashboard mock now sends `measured_mw` / `power_source` exactly as A-F10 does (null for simulated chargers), and `--mock-pi-mw` is gone. Closes L37.
- **What:** the Pi sends its INA219 milliwatts in each meter reading, and the server stores them as `measured_mw` on the station row. The Pi tile then shows, for example, "measured: 23.4 mW".
- **Needs first:** A and B agree the one-line format (see L37); B-F8 ✅.
- **Order:** agree the format, then B-F10 (agent side) and A-F10 (server metering + `StationView.measured_mw`) in parallel. If the agent change falls outside the files Track B may edit, send the diff to Track C.
- **Unblocks:** the number in scene 6. The dashboard is already ready for it. **Optional:** if it is skipped, show the Pi's console during scene 6.

**3. C-F5 part 2 — one run for a fleet started in two parts (L38)** ✅ (Track C L42/L38 PR, 2026-10-12; L38 stays open until it is checked on the real halt evidence in step 15) · Koshambi (C)
- **What:** when one run is started as two load generators (the halt run, the demo), the analysis joins the two diaries into one run. The halt then appears once, with N = 50.
- **Needs first:** nothing. Built against made-up diaries; checked on the real halt evidence in step 15.
- **Unblocks:** step 15.

**4. A-F8 (draft) — demo runbook** ✅ (v1.0 = `docs/Demo_Runbook.md`, written by Track C for Track A, 2026-10-12: every command checked against `main` `034bcb4`, one-line description per command, stale v0.1 items fixed; the ⟦blanks⟧ — hotspot IP, Pi login, names — are filled at the first rehearsal, step 17. **v1.2, 2026-10-14 (Track C):** the demo is 8 variants shown one by one — V1 normal charging (44 + 4 legacy + CP0000), V2 migration from the browser, V3 attack charger CP0045 (forged certificate, wrong identity, late join + migration, stolen certificate), V4 storm, V5 rotation, V6 halt, V7 curtailment, V8 results; checked on a real hybrid TLS server on Linux, not yet on the Windows laptop. Goes with the redesigned dashboard (empty until chargers connect, a tile per charger, colours per state, wave replay, summary card, "Migration, live" chart, attack tiles), the server's TLS-refusal line (`csms/server.py`, `csms/transport.py`), the Pi's migration blink (`agent/gpio_power.py`, `agent/station.py`) and `experiments/forge_cert.py`. **v1.3, 2026-10-14 (Track C):** after the `demo2` rehearsal — `$IP` / `$RUN` in every window, the checked command order (`Test-Path`, `$stamp` archive), `--no-analyse` on every load generator, the analysis of the run after V7 (`python -m analysis.run --run $RUN`) and the results page as V8; `$RUN`/`${RUN}_…` spelling not yet typed on the Windows laptop) · Bhargavi (A)
- **What:** one page with the exact commands per scene: windows, ports, fleet profiles, the `--db` + `certs\pq` reset, the Pi command and the expected screen. Plus a 3-line fix for each likely failure. Reuse `experiments/e5_attack.md` (scene 5) and `dashboard/README.md`.
- **Needs first:** nothing to start. It is finalised after steps 9 and 17.
- **Unblocks:** step 17.

**5. L34 — Track C's yes or no** ✅ (2026-10-12: yes — L34 agreed by A, B and C, closed) · Koshambi (C), a decision only
- **What:** L34 (a passed migration key check also marks that connection verified) is waiting only for Track C's answer.
- **Needs first:** nothing.
- **Unblocks:** removing L34; one sentence for Contract 7 §7.7.

### Stage 2 — demo set-up, split so the hardware does not block the software (restructured 2026-10-13)

**Two tracks from here.** Software (2S) runs today, on Koshambi's laptop, with real data from the real server; no Pi needed. Hardware (2H) starts tomorrow, when the Pi is wired. The Pi is just one more charger (CP0000), so it joins whichever software fleet is running, without changing anything else. **Steps 8, 13 and 15 do not need the Pi either.**

| Order | Software, today (no Pi) | Hardware, tomorrow |
|---|---|---|
| 1 | 6 ✅ → **6a** certificates for 500 | — |
| 2 | **6b** software rehearsal at 50 (real data; a simulated stand-in for CP0000) | 7 Pi bring-up (needs 6, 6a) |
| 3 | **6c** 500-charger build (needs a team OK) → **6d** software rehearsal at 500 | 10 the Pi in the live fleet (needs 7, 6b or 6d) |
| 4 | 8 tests, 13 experiment runs → 15 results page (any time, one thing at a time on the laptop) | — |
| 5 | 16 clean-up, 17 dress rehearsal with the Pi (needs everything) | ← |

#### Stage 2S — software (today)

**6. A-F6 — the server on the LAN (Koshambi's laptop is the demo server)** ✅ (2026-10-13, laptop side; done by Koshambi for Track A) · Bhargavi (A), with Koshambi
- **Done:** iPhone hotspot (Maximize Compatibility on); laptop IP **172.20.10.6**; new certificates with that IP in the server certificate; firewall rule `pqcharge` (TCP 9000, inbound, every profile); hybrid server on `0.0.0.0:9000` over mutual TLS.
- **Checked:** `/api/health` over `https://172.20.10.6:9000` with the Pi's own certificate → `tls: true`, `identity_check: enforce`, `boot_check: true`; a test charger as CP0000 over the IP → TLS 1.3, `identity_ok: true`, booted and charging; Safari on the iPhone reached the port (TLS refused without a client certificate — the expected answer).
- **Left for step 7:** the Pi's own `curl` to `/api/health` — the only true test of the firewall (the laptop calling its own IP skips the inbound rule).
- **Found:** Windows `curl.exe` (Schannel) cannot load a PEM client certificate, so the `api` helper in runbook §3 W6 and `experiments/e5_attack.md` fails on this laptop. **Working helper (PowerShell):**
  ```powershell
  # GET an /api path from the server with a CA-signed client certificate (Python instead of curl.exe).
  function api($p) { $env:API_PATH = $p; python -c "import os, urllib.request, ssl; c=ssl.create_default_context(cafile='certs/root.pem'); c.load_cert_chain(certfile='certs/CP0049.crt.pem', keyfile='certs/CP0049.key.pem'); r=urllib.request.urlopen('https://localhost:9000'+os.environ['API_PATH'], context=c, timeout=10); print(r.read().decode())" }
  ```
- **The hotspot IP can change between days:** run `ipconfig` first; if it is not 172.20.10.6, redo the certificates (6a) and copy the Pi's three files again.

**6a (new). Certificates for every run size** ✅ (2026-10-13, `--count 500 --also CP0000 --server-san 172.20.10.6`) · Koshambi (C), 5 min, **today, before step 7 copies files to the Pi**
- **What:** today's certificates cover only CP0001–CP0050 + CP0000, but E1/E2 run at N = 100 (step 13) and the 500-charger demo (6c) needs CP0001–CP0499. Make them once, for everything: `python -m experiments.bootstrap_pki --count 500 --also CP0000 --server-san 172.20.10.6`.
- **Needs first:** step 6. **Stop every server first**; afterwards archive `certs\pq` and use a new `--db` (the root CA is new).
- **Unblocks:** 6b, 6d, 7, 13.

**6b (new). Software rehearsal at 50, real server, real data** ☐ **— to redo** (checked 2026-10-14 from the `sw1` logs: the first session, 13 Oct, migrated 46 / 4 incompatible correctly; but the two later sessions restarted the server with the **same `sw1` `--db`** after `certs\pq` had been archived in 6a, so the server restored 46 keys the chargers no longer had, and **every** charger's boot check failed: 6,066 checks rejected, 6,066 closes with 1008 — the key-folder / `--db` mismatch of runbook §1c, not an attack. Scenes 3–7 and the rotation were not reached. **Redo with a new run name (`sw2` → new `--db`) and an empty `certs\pq`.**) · Koshambi (C), A and B welcome
- **What:** runbook §3 and §3b exactly, except: the Pi line is replaced by a simulated stand-in for CP0000 on the laptop (same id and certificate, `--power sim`), and W6 uses the Python `api` helper above. Then scenes 1–5 and 7 on tab 8080/8081, and scene 6 without the LED (fleet power still drops and recovers).
- **Pass:** dashboard shows 50 connected with no MOCK badge; migration canary → waves; the halt stops at wave 2 (15 / 10 / 25); storm → every migrated charger boot-checked; impostor cut off with 1008; rotation with no disconnect.
- **Also closes L39 and L40** if: `python -m experiments.e5_check logs\<run>_events.jsonl` gives the same verdicts as the results page's E5 for the same log, and `/api/migration` rotation counts equal the `rotation_completed` / `rotation_failed` lines.
- **Needs first:** 6a. **Unblocks:** 6d, 10, 17.

**6c (new, agreed by A, B, C 2026-10-13). The 500-charger demo build** ✅ (Track C PR, 2026-10-13: the profile, the dashboard's dense grid + rotation colours, runbook v1.1 §3c; checked with the real server at 499 chargers over TLS on a test machine: migration ~5 s, storm ~10 s, rotation ~10 s) · B (profile), C (dashboard, runbook)
- **Why:** to show the panel the whole fleet migrating at scale — 499 simulated chargers + the Pi. S1 showed one laptop holds 500 chargers (CPU p95 19 %, no TLS); the dashboard today draws 500 full-size tiles (a 3,300-px page, so the wave sweep cannot be seen on one screen).
- **What:**
  1. Track B: `tests/fixtures/fleet_profile_demo_n500.json` — CP0000 + CP0001–CP0449 capable, CP0450–CP0499 legacy (10 %).
  2. Track C: the dashboard draws small colour squares instead of numbered tiles when the fleet has more than 100 chargers (the Pi tile stays large; click still opens the inspector).
  3. Track C: runbook §3 variant for 500 (load generators `--n 44` + `--n 454 --start-index 46`, victim CP0045 unchanged; canary 10, waves of 50; `--watch-machine` on).
- **Needs first:** a team OK (given 2026-10-13), 6a.
- **Unblocks:** 6d.

**6d (new). Software rehearsal at 500** ☐ · Koshambi (C)
- **What:** 6b at 500 chargers: runbook §3c (W1 with `fleet_profile_demo_n500.json`, W2 `--n 44`, W2b `--n 454 --start-index 46 --watch-machine`), then the saturation check in §3c.
- **Pass:** as 6b, plus the laptop not saturated (`machine_saturated` off: CPU p95 < 85 %, loop lag p95 < 50 ms) and the storm recovered within about a minute. **If not, the demo stays at 50** — every step works at both sizes.
- **Needs first:** 6c. **Unblocks:** 17 (decides the demo size).

#### Stage 2H — hardware (tomorrow; blocks only steps 10 and 17)

**7. B-F9 — Pi bring-up** ☐ (commands: runbook §1b and §3; software prepared: `scripts/pi_setup.sh` does every install step below, incl. the F8 folder trap, and `python -m agent.gpio_power` checks LED + INA219 without a server) · Amal (B); the wiring is done by the team
- **What:** install Raspberry Pi OS, clone the repo, create a venv and install `requirements.txt`. Install quantcrypt and compile it (**without** `--with-opt`), then run the 1312 check. Copy `certs/CP0000.*` and `root.pem` to the Pi. Run `bash scripts/pi_charger.sh <laptop-ip>`.
- **Needs first:** steps 6 and 6a (copy the certificates made in 6a); LED + INA219 wired; `gpiozero` and the INA219 library installed on the Pi (they are not in `requirements.txt`).
- **Unblocks:** step 10; scenes 1 and 6. Closes L13.

**10. T-3 (new) — the Pi in the live fleet** ☐ · team
- **What:** the Pi joins over TLS. Its tile is visible. It migrates in the canary (CP0000 sorts first) and passes its boot checks. The LED dims when the limit slider is moved.
- **Needs first:** steps 7 and 9, and a software fleet running (6b or 6d) — the Pi simply joins it.
- **Unblocks:** step 17.

### Stage 3 — tests and integration (moved here, after building)

**8. T-1 (new) — the missing happy-path tests** ☐ (**Track C ✅ 2026-10-14:** `tests/analysis/test_cf5_part2_split_runs.py` (step 3, L38), `tests/analysis/test_e5_victim_and_verdicts.py` (PR #46: victim outside the load generator; same verdicts as `experiments/e5_check`), `tests/harness/test_cf7_watch_rotation.py` (C-F7), `tests/dashboard/test_dashboard.py` +4 (6c: 500 profile = mock, waves of 50, dense-grid wiring, `app.js` syntax), `tests/crypto/test_bootstrap_overwrite.py` (bootstrap replaces old certificates); **+16 for runbook v1.2:** `tests/csms/test_tls_refusal_event.py` (4: forged / missing certificate → one `tls_rejected` line each, good certificate → none, a broken callback never weakens the refusal), `tests/agent/test_led_blink.py` (6: blink, back to on / off / dimmed, a real enrolment blinks and a boot check does not), `tests/dashboard/test_dashboard.py` +6 (gradual connect, attack mocks = the server's lines, page wiring); **+11 for v1.3:** `tests/analysis/test_demo_runs.py` (only the named run's files, evidence copy never takes `.log`/`.db`, main and halt apart, re-analysis replaces and adds nothing, analysis from evidence, the automatic analysis never takes the page off the demo run, combined = side by side + pooled, a part started twice is not chained, page wiring, `charts.js` syntax); Track B ✅: B-F5 in `tests/crypto/test_pq_x509.py`, B-F10 in `tests/agent/test_gpio_power.py`, B-F1b in `tests/idmanager/test_orchestrator.py`; Track A ✅: A-F10 in `tests/csms/test_a_f10_measured_mw.py`. Full suite 746 passed, 2026-10-14) · each track, for its own code
- **What:** one "it works" test per feature that was built without one:
  - Track B: B-F5;
  - Tracks B and A: B-F10 / A-F10;
  - Track C: step 3, plus PR #46 (E5 victim counting, the rotation view in `watch_migration`).
  - Then run the full suite on `main`.
- **Needs first:** Stage 1.
- **Unblocks:** step 9.

**9. T-2 = C-F1 — the A+B+C re-test, live** ✅ (2026-10-12, reported passed by Track B; Track C to add the evidence path) · Koshambi runs it; A and B attend
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

### Stage 4 — remove the old key flow (after the re-test)

**11. A-F3 ✅ + B-F1b ✅ (PR #49, merged after step 9) — the server and orchestrator stop using `InstallPQAuth`** · Bhargavi (A) and Amal (B). **A-F3 was done in Track C's L42 PR (2026-10-12), by team decision, together with dropping the orchestrator's last "accepted and ignored" keywords** (`keypair_factory`, `install_message_factory`, now refused) — Track A and Track B please review.
- **What:** delete the `keypair_factory` / `install_message_factory` path from `csms/migration.py` and `idmanager/orchestrator.py`, and make the new pair required.
- **Also update their own users of the old builder:** Track A's `tests/csms/test_log_redaction.py` imports `build_install_message`; Track B's `experiments/rotation_harness.py` mentions it.
- **Needs first:** step 9 passed.
- **Closes:** L04 (together with step 12) and the last part of L01.
- **Unblocks:** step 12.

**12. C-F9 (new) — the charger drops `InstallPQAuth`** ✅ (2026-10-12, same PR as step 11's A-F3) · Koshambi (C)
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
- **Needs first:** Stage 4 (the runs use the final code); step 9; 6a (certificates for N = 100). **Does not need the Pi** — can run today, but not at the same time as a rehearsal (the laptop must run one thing at a time for clean timings).
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

**16. Register clean-up (A-F9, B-F7, C-F8)** ☐ (Track B part 1 ✅ 2026-10-12: L02, L32 closed; Track C 2026-10-12 ✅: L01, L04, L05, L31, L34, L37, L42 closed; left: L09, L11, L13, L38–L40 after steps 7, 9's evidence, 13, 15) · all
- **What:** remove every L item that Stages 3–5 closed (already gone: L01, L02, L04, L05, L31, L32, L34, L37, L42, L43; left: L09, L11, L13, L38–L40).
  - Add the L31 and L34 sentences to Contract 7 (§7.6, §7.7) and note in §7.8 that `InstallPQAuth` is removed.
- **Needs first:** Stages 3–5.

**17. Dress rehearsal and freeze (A-F8, CP4)** ☐ · all; Bhargavi leads
- **What:** run scenes 1–8 twice on the demo laptop + Pi, following the runbook (`docs/Demo_Runbook.md`). Fix only what breaks. Freeze `main` and tag `v1.0`.
- **Needs first:** everything above.

---

## 1. Blocking E1 / E2 (all decided; built through Contract 7)

*Empty since 2026-10-12: L01 and L02 are closed (see "Resolved and removed").*

| No. | Finding | Status | Action | Owner |
|---|---|---|---|---|

## 2. Agreed fixes, not yet done

| No. | Finding | Status | Action | Owner |
|---|---|---|---|---|
| L09 | **E3 showed the rollback but not the halt.** The failing chargers were in the last wave. | Agreed | One extra E3 run with the refusers mid-fleet (for example CP0021–25): a new fleet profile, plus the load generator run twice with `--start-index`. **Profile ready (B-F4, 2026-10-10):** `tests/fixtures/fleet_profile_halt_n50.json` (refusers CP0021–25; expected phase `rolled_back`: migrated 15, rolled_back 10, pending 25 — checked against the real orchestrator). | C runs; B (profile contents), A (fixture) |
| L11 | **Session runs were stopped with Ctrl-C,** so they are marked "incomplete". | Agreed | Every run ends on its own. `--charge-for` follows the rule in Track C's plan (ramp + action + 30 s observation + 30 s margin). | C |

## 3. Planned (in the owner's plan)

| No. | Finding | Status | Action | Owner |
|---|---|---|---|---|

## 4. Not decided

| No. | Finding | Status | Action | Owner |
|---|---|---|---|---|
| L13 | **Raspberry Pi charger node: setup checks still open.** Hardware is **agreed and bought (2026-10-09):** Raspberry Pi + INA219 sensor + an LED with a 330 Ω resistor on **GPIO18 (PWM)**, no relay. The LED stands for the charging power: on/off = contactor closed/open, brightness (PWM duty) = power limit. | Agreed (hardware); checks open | On the Pi: Track A's quantcrypt 1312 check; certificate SAN for the laptop's `.local` name (old B-R4). Track C builds `GPIOPower` (C9) behind Contract 5. How the INA219 reading becomes an OCPP meter value: **L32**. **Update 2026-10-11 (Track C, from `main`):** the Pi software moved to Track B and is built (B-F8: `agent/gpio_power.py`, `--power gpio`, `scripts/pi_charger.sh`); the Pi's id is **CP0000** (in `fleet_profile_demo.json`); certificates with the hotspot IP come from `bootstrap_pki --also CP0000 --server-san <ip>` (B-F3). **Open:** the bring-up on the Pi itself (B-F9: 1312 check, certificates, `gpiozero` + INA219 library, which are not in `requirements.txt`) and the server on the LAN (A-F6). Tracker §0, steps 6, 7, 10. **Update 2026-10-12 (Track B):** the whole Pi install is one script, `bash scripts/pi_setup.sh` (system `gpiozero`/`lgpio`, I2C on, venv, `pi-ina219`, quantcrypt compiled without `--with-opt` with the F8 folder made first, 1312 check, crypto tests, `i2cdetect`), plus a hardware self-test without a server, `python -m agent.gpio_power` (PR #49). The Contract 7 flow for CP0000 was checked on the laptop with `--power sim` (enrolment, then a boot check after restart, both `pq_auth` success). **Open:** running it on the Pi itself (B-F9). | B (B-F9); A (A-F6) |

## 4b. New items (2026-10-08)

| No. | Finding | Status | Action | Owner |
|---|---|---|---|---|

## 4c. New items (2026-10-09)

| No. | Finding | Status | Action | Owner |
|---|---|---|---|---|

## 4d. New items (2026-10-10, Track A)

| No. | Finding | Status | Action | Owner |
|---|---|---|---|---|

## 4e. New items (2026-10-10, Track B)

| No. | Finding | Status | Action | Owner |
|---|---|---|---|---|

## 4f. New items (2026-10-10, Track C)

| No. | Finding | Status | Action | Owner |
|---|---|---|---|---|

## 4g. New items (2026-10-11, Track C)

| No. | Finding | Status | Action | Owner |
|---|---|---|---|---|
| **L38** | **A fleet started as two load generators shows up as two runs on the results page.** The halt run (`--n 20` + `--n 25 --start-index 26`, profile `fleet_profile_halt_n50.json`) and the demo fleet (`--n 44` + `--n 4 --start-index 46`) each write two tester diaries. The analysis makes one run per diary, and migration lines are fleet-wide, so the same halt appears twice: as an E3 run with N = 20 and another with N = 25, each showing the whole migration. | **Built (2026-10-12)**; check on real evidence left | Track C, C-F5 part 2 (tracker step 3): `analysis/collect.py` `join_split_runs()` joins tester runs that share mode and TLS, overlap in time, started no charger in common and have matching experiment names (`e3halt` + `e3halt`, or `demo` + `demo_legacy`) into ONE run with N = their sum; the page's Runs table says "2 load generators: 20 + 25" (`joined_from`, RESULTS_FORMAT). **Checked only on made-up diaries so far** (the halt appears once, N = 20 + 25 = 45). **Remaining:** check it on the real halt evidence (step 15), then remove this item. **Run both halves of a split run with the same `--experiment` name** (or the second as `<name>_<suffix>`). **Fixed 2026-10-14 (found in the real `demo2` halt logs):** the same part started twice in two windows was chained into one run of 70; now no two parts of a joined run share a charger, and the copy that ran longer alongside wins (`tests/analysis/test_demo_runs.py`). | C |
| **L39** | **Track C's PR #46 has been checked only against the dashboard mock and Track A's API tests, not against a live server.** Not yet run on a real hybrid server: the E5 victim counting, the `boot_check` chip, the CP0000 Pi tile and the rotation view in `watch_migration`. | Planned | The A+B+C re-test (C-F1, tracker step 9): dashboard and `watch_migration` open while migrating, rotating and running the E5 small run. The E5 verdicts from `analysis` must equal `python -m experiments.e5_check` on the same log. Remove when that passes. **Update 2026-10-12 (Track C):** the A+B+C re-test (step 9) was reported passed by Track B, but its evidence is not in the repo yet, so it cannot be confirmed that the dashboard, the E5 verdicts and the rotation counts were checked during it. Close this item when that evidence (the run's `*_events.jsonl` + tester diaries) is added and checked, or after the step 13 runs. | C (A, B attend) |
| **L40** | **The rotation counts are read as Track B describes them; no live rotation has been watched yet.** The dashboard, `watch_migration` and E3 read a rotation's `MigrationStatus` as `migrated` = rotated and `rolled_back` = kept its old key (`idmanager/api.py`, `kind: "rotation"`). | Planned | Check during rotation at N = 10 (tracker step 9) and in the E3 rotation run (step 13): `/api/migration` counts must equal the `rotation_completed` / `rotation_failed` lines. **Update 2026-10-12 (Track C):** the A+B+C re-test (step 9) was reported passed by Track B, but its evidence is not in the repo yet, so it cannot be confirmed that the dashboard, the E5 verdicts and the rotation counts were checked during it. Close this item when that evidence (the run's `*_events.jsonl` + tester diaries) is added and checked, or after the step 13 runs. | C (B confirms) |

## 4h. New items (2026-10-14, Track C — runbook v1.2)

| No. | Finding | Status | Action | Owner |
|---|---|---|---|---|
| **L44** | **The server's TLS-refusal line is checked on Linux only.** `csms/transport.py` `observe_tls_refusals()` wraps `ssl.SSLContext.sslobject_class`, which asyncio's TLS layer uses on every platform; on Windows (proactor event loop) this is expected but **not yet seen working**. If it does not fire, the refusal itself still happens — only the `connection_failed` / `tls_rejected` line and the dashboard's "?" tile are missing. | Planned | At the first Windows rehearsal (6d / 17), runbook V3 (a): the "?" tile appears and the server log has "TLS handshake refused". Remove this item when seen. | A (C runs it) |
| **L45** | **A device refused during TLS has no charger id and no address in the log.** The handshake fails before the charger sends its id (it is in the URL), and the hook sees only the TLS error. So a forged "CP0045" certificate shows as an **unknown device** ("?" tile), not as a red CP0045 tile; the wrong-identity and stolen-certificate attacks do name CP0045. | Not decided (recommend: Accepted) | Say it in the demo (runbook V3 a). Adding the client's IP would need the transport's peer address in the hook — possible, not built. | A, C |
| **L46** | **A second migration run enrols every capable charger again, not only the ones still pending.** `idmanager/fleet_adapter.py` `migration_candidate_ids()` returns every station; already-migrated chargers make a new key and are checked again (seen 2026-10-14: the late CP0045 run did 46 key checks, the Pi's LED blinked again; in `demo2` Start was pressed twice 5 s apart, so 3 migration runs and 136 migration key checks for 46 chargers). Harmless for the demo (tiles stay purple), but it is not "the late joiner only", and E3 counts of a repeated run include them. The dashboard mock only migrates pending chargers. | Not decided | Track B: keep (simple, re-proves everyone) or skip chargers already `migrated` in a migration run (rotation exists for new keys). Runbook V3 (c) says what happens today. | B |
| **L47** | **The dashboard replays the waves for the eye.** The real orchestrator finishes a wave in well under a second; the page shows each wave blue for at least 1.2 s, in order, and the "Migration, live" chart counts the tile colours (so it follows the replay). The page says "Replaying…", and the summary card shows the server's real time. | Not decided (recommend: Accepted) | State it next to any dashboard screenshot used in the report. All measured numbers come from the logs, never from the page. | C |
| **L48** | **E5 has no verdict for TLS refusals.** `experiments/e5_check` and the results page's E5 give three verdicts (stolen certificate, wrong identity, curtailment). The forged / missing-certificate refusals (`connection_failed`, reason `tls_rejected`) have no charger id, so E5's "Connection failures" tile (per charger) never counted them — **corrected 2026-10-14:** E5 now reports them separately as `tls_refusals` ("Refused at TLS" tile; V3 row of a demo run). A fourth verdict would change `e5_check` and the page at once (they must stay equal, L39). | Not decided | Decide at step 13 whether `e5_check` and E5 get a fourth verdict ("forged certificate refused"). | A, C |

| **L49** | **On the laptop, a charger takes about 2 s to connect to the main server, but about 20 ms to the halt server.** Seen in `demo2`: connect median 2,039 ms (main, 48 chargers) vs 19 ms (halt); the results page's pooled connect curve has the same two steps. The main server is started with `--host 0.0.0.0` (IPv4 only, needed for the Pi); the halt server with the default host. **Likely cause (NOT VERIFIED):** Windows tries `localhost` as IPv6 (`::1`) first, waits about 2 s, then falls back to IPv4. It inflates E1-style connect times in demo runs only; key checks, migration, storm and rotation times are not affected. | Not decided | Test at the next rehearsal: one load generator with `--csms-url wss://$IP:9000` (the server certificate already names `$IP`). If its connect median drops to tens of ms, use `wss://$IP:9000` for every laptop charger in the runbook. | C (A advises) |
| **L50** | **Each demo run committed as evidence adds about 25–30 MB to the repository** (`demo2`: 27 MB; the `--watch-fleet` diary is 21 MB of it; the 500-charger layout is larger). | Not decided | Commit only the runs worth keeping (`git add "evidence/demos/<name>"`), or drop `--watch-fleet` from W2 — **check first**: E3's state timeline and E2's snapshot curve are drawn from those snapshots. | C |

## 5. Accepted limitations (stay in the report)

| No. | Limitation | Why it is accepted |
|---|---|---|
| L20 | **Post-quantum protection is at the application layer only (Option B).** TLS stays classical X.509, so the OCPP traffic itself is not post-quantum encrypted. | Post-quantum TLS cannot be loaded into Windows CPython (docs R3), and `ssl` cannot use ML-DSA certificates in a handshake (`cryptography` 50+ can build them, which E4 uses to measure their size, B-F5). Keeps third-party chargers (E6) working. |
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
| L02 (the security mode was only a label: the key was checked once, at migration) | Contract 7 §7.5–7.7: Track B B-P2 (boot verifier, PR #37) + Track A A-F1 (`--mode hybrid` checks every boot, 1008 on failure, PR #42); A+B+C re-test passed 2026-10-12 (tracker step 9) |
| L32 (the Pi's INA219 reads milliwatts, not kilowatts) | Decided (a) scaled, FINAL 2-DAY PLAN; built in B-F8 (`GPIOPower.read_power()` = LED duty × 7.4 kW). The raw milliwatts on the dashboard are L37 |
| L01 (the server made every charger's private key and sent it over the network) | Contract 7 §7.2–7.4: Track C C-P1/C-P2 (the charger makes and saves its own key), Track B B-P1 (enrolment by public key, PR #32), Track A A-P4 (wiring, PR #36); A+B+C re-test passed 2026-10-12 (step 9); the old path removed from every track 2026-10-12 (B-F1b PR #49; A-F3 + C-F9 in Track C's L42 PR). `docs/limitations.md` I5 |
| L04 (private keys appeared in the server log) | Track A A-P5 log filter (`csms/log_redaction.py`, kept as a safety net); root cause `InstallPQAuth` removed from every track 2026-10-12 (L42). `docs/limitations.md` I6. The GitGuardian incident (action 3 of the old item) is **closed** as a test credential (confirmed by Koshambi, 2026-10-12). |
| L34 (a passed migration key check also marks that connection verified) | Track A PR 2 (`orchestrator_emitter(on_pq_auth_success=registry.mark_pq_verified)`); **agreed by A, B and C 2026-10-12**. One sentence for Contract 7 §7.7 with the next revision (tracker step 16). `docs/limitations.md` I7 |
| L37 (the Pi's measured milliwatts never reached the server) | Track B B-F10 (customData `pqcharge.hw` on the power sample, PR #48) + Track A A-F10 (`StationView.measured_mw` / `power_source`, PR #51); the dashboard shows the number on the Pi tile, and its mock sends the same fields. `docs/limitations.md` I4 |
| L42 (the charger still understood `InstallPQAuth`; no task removed it) | 2026-10-12, after the re-test: B-F1b (orchestrator, PR #49); Track C's L42 PR — A-F3 (`csms/migration.py` no longer passes `keypair_factory` / `install_message_factory`; the orchestrator now refuses them), C-F9 (agent handler, builder, parser and `PQIdentity.install()` deleted; an `InstallPQAuth` that arrives is answered UnknownMessageId), tests moved to the Contract 7 flow (Track A's `test_log_redaction.py` builds its frame by hand) |
| L05 (coarse timers on Windows before Python 3.13) | All three tracks on Python 3.13: Track A 3.13.16 (Mac, 2026-10-10), Track B 3.13.5 (Track B plan R6), Track C 3.13 (the session laptop, S1 and every run since); the agent itself uses `time.perf_counter()` (PR #26). Closed 2026-10-12 |
| L31 (where `source` in the post-quantum events comes from) | Agreed A + B, Track C informed; built in Track A PR 2: `orchestrator_emitter(event_log, source=...)` writes `source` (`orchestrator` / `boot_verifier`), and Track B's code never puts it in a payload. Every live diary since shows it. One sentence for Contract 7 §7.6 at the next revision (tracker step 16). Closed 2026-10-12 |
