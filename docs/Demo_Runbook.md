# PQCharge — Demo Runbook (A-F8, tracker step 4)

**v1.1, 2026-10-13** (v1.0: 2026-10-12). Written by Track C (Koshambi) for Track A's task A-F8, from the v0.1 draft (Track A, 2026-10-11). Every command below was checked against `main`: the flags exist and the files are where it says. Lead on the day: Bhargavi.

**What changed in v1.1 (tracker steps 6–6c):**

- **Every load generator now says `--crypto-mode hybrid`.** Without it the tester diary is labelled `classical` (file `<run>_n<N>_classical.jsonl`) while the server is hybrid, and the analysis flags a mode mismatch. Behaviour is unchanged; only the label.
- **Two fleet sizes:** the 50-charger layout (§3) and the **500-charger layout** (§3c: 499 simulated chargers + the Pi). The dashboard switches to small squares above 100 chargers and pre-fills canary 10 / wave 50.
- **`api` helper (W6) uses Python, not `curl.exe`:** Windows `curl.exe` cannot load our PEM client certificates (found in A-F6).
- **No Pi yet?** §3 has a simulated stand-in for CP0000, so the software fleet can be rehearsed without the hardware.
- **Certificates for up to 500 chargers** (`--count 500`), made once.
- **Koshambi's laptop runs global Python 3.13** (no venv); the venv lines are only for machines that use one.

**What changed in v1.0 (from v0.1):**

- The demo laptop is decided: Koshambi's.
- Scene 6 now shows the Pi's real milliwatts **on the dashboard tile**; L37 is closed.
- The Pi is started with a 5 s reconnect ceiling, so it comes back as fast as the fleet in scene 4.
- The halt fleet's two load generators share one experiment name (L38).
- The console view (W5) is restarted before the rotation: it exits by itself when a migration ends.
- Pi set-up uses `scripts/pi_setup.sh`, plus the hardware self-test.
- Certificates are copied to the Pi with `scp`.
- The demo logs are marked **not evidence**.
- Every command has a one-line description.

> **Fill in these blanks before the first rehearsal** (they are marked **⟦like this⟧**):
> - **Demo laptop:** Koshambi's Windows laptop (decided). Commands are **PowerShell**. On a Mac, use `/` for `\` and `curl` for `curl.exe`, and skip the firewall step.
> - ⟦LAPTOP-IP⟧: the laptop's IP on the phone hotspot (iPhone hotspot, 13 Oct: **172.20.10.6** — check with `ipconfig` each day; if it changed, redo the certificates and copy them to the Pi again).
> - ⟦PI-USER@PI⟧: how we SSH into the Pi (e.g. `pi@raspberrypi.local`).
> - ⟦PI-REPO⟧: the repo folder on the Pi (e.g. `~/PQCharge`).
> - ⟦RUN⟧: a new name per rehearsal (`demo1`, `demo2`, `final`).
> - ⟦time⟧: any timestamp for archive folder names (e.g. `1830`).

---

## 0. The storyline (≈ 12 min) and who does what

| # | Scene | ~min | Shown on | Proves |
|---|---|---|---|---|
| 1 | Fleet up: 49 (or 499) simulated chargers + the Pi (CP0000), mutual TLS 1.3 | 1.5 | Dashboard (tab 8080) | Classical baseline; real hardware in the fleet |
| 2 | Migration to hybrid (canary → waves) | 2 | Dashboard + W5 | Live migration, sessions not lost (core claim) |
| 3 | Halt: a bad wave is rolled back, the migration stops | 1.5 | Dashboard (tab 8081) | Safety: threshold, rollback, halt |
| 4 | Reconnect storm: every charger re-proves its key | 1.5 | Dashboard | Hybrid = a real check at every boot |
| 5 | Attack: stolen certificate refused (1008) | 1.5 | Dashboard + W3 | A classical identity alone is no longer enough |
| 6 | Curtailment: the Pi's LED dims, its mW drop | 1 | Dashboard + the LED | Cyber-physical loop |
| 7 | Key rotation without disconnection | 1.5 | Dashboard + W5 | Novelty claim |
| 8 | Results page | 1.5 | `report.html` | The numbers |

- **Driver** (types commands): Bhargavi.
- **Narrator** (talks to the panel): ⟦name⟧.
- **Pi minder** (watches the Pi console and the LED): ⟦name⟧.
- **Fleet size:** 50 (§3) or 500 (§3c). Decided by the 500-charger rehearsal (tracker step 6d): 500 if the laptop is not saturated, else 50. Every scene works at both sizes; scene 3 (halt) always uses its own 50-charger fleet.

---

## 1. One-time set-up (the evening before)

### 1a. Laptop

```powershell
# Get the frozen code (tag v1.0 after CP4; main until then).
git checkout main; git pull origin main
# ONLY if this machine uses a venv: turn it on (do this in EVERY window). Koshambi's laptop uses global Python 3.13: skip.
.\.venv\Scripts\Activate.ps1
# Must print 3.13.x (L05: every track is on 3.13).
python --version
# ML-DSA works on this laptop: must print 1312. If it fails, the hybrid server will refuse to start.
python -c "from crypto.pq import PQProvider; print(len(PQProvider().generate_keypair()[1]))"
# The whole test suite: all must pass. Run it with NO server running.
python -m pytest -q
# Find the laptop's hotspot IP (Wireless LAN adapter -> IPv4 Address); write it into ⟦LAPTOP-IP⟧.
ipconfig
```

**Certificates.** This makes a **new** root CA, so run it **once** and copy to the Pi afterwards:
```powershell
# Certificates for CP0001-CP0500 (both fleet sizes and the N = 100 experiment runs) + the Pi (CP0000); the server certificate also covers the hotspot IP.
python -m experiments.bootstrap_pki --count 500 --also CP0000 --server-san ⟦LAPTOP-IP⟧
```

**Firewall** (once, in an **admin** PowerShell):
```powershell
# Let the Pi reach the server on port 9000.
netsh advfirewall firewall add rule name=pqcharge dir=in action=allow protocol=TCP localport=9000
```

**Results page** (once the final evidence is merged, tracker steps 13–15):
```powershell
# Build the results page from the final evidence; opens as analysis\output\report.html.
python -m analysis.run --logs evidence\2026-10-12_final
```

**Browser:** two tabs, `http://localhost:8080/dashboard/` (main) and `http://localhost:8081/dashboard/` (halt). Full screen (F11), zoom about 125 %.

### 1b. Raspberry Pi (Pi minder; wiring done by the team)

On the Pi, from ⟦PI-REPO⟧:
```bash
# One-time install: system GPIO packages, I2C on, venv, requirements, INA219 library, quantcrypt build, 1312 check, crypto tests.
bash scripts/pi_setup.sh
# Turn on the venv (do this in every new Pi terminal).
source .venv/bin/activate
# Hardware self-test without a server: LED on/dim/off and INA219 readings.
python -m agent.gpio_power
```

From the laptop:
```powershell
# Copy the Pi's certificate, key and the root CA (from THIS bootstrap_pki run) to the Pi.
scp certs\CP0000.crt.pem certs\CP0000.key.pem certs\root.pem ⟦PI-USER@PI⟧:⟦PI-REPO⟧/certs/
```

Once the main server (§3) is running, from the Pi:
```bash
# The Pi reaches the server over mutual TLS: expect JSON with "tls": true.
curl --cacert certs/root.pem --cert certs/CP0000.crt.pem --key certs/CP0000.key.pem https://⟦LAPTOP-IP⟧:9000/api/health
```

---

## 1c. Before every rehearsal and the real demo (fresh state)

The chargers' key folders and the server's `--db` file describe the **same** keys. Reset them **together**. Otherwise the server (correctly) refuses chargers whose keys it doesn't know, and that looks like an attack.

```powershell
# Archive the main fleet's charger keys (skip if the folder does not exist).
move certs\pq certs\pq_old_⟦time⟧
# Archive the halt fleet's charger keys (skip if it does not exist).
move certs\pq_halt certs\pq_halt_old_⟦time⟧
```
```bash
# On the Pi: archive the Pi's key (skip if it does not exist).
mv certs/pq certs/pq_old_⟦time⟧
```
Then pick a **new** ⟦RUN⟧ name, so the server gets a new `--db` and new log files.

---

## 2. Window layout (laptop)

| Window | What runs | Started in |
|---|---|---|
| **W1** | Main server (port 9000, hybrid, TLS, demo profile) | §3 |
| **W2** / **W2b** | Main fleet CP0001–CP0044 / legacy CP0046–CP0049 | §3 |
| **W3** | The **victim** CP0045, alone (needed for scene 5) | §3 |
| **W4** | Dashboard proxy → `localhost:8080` (main) | §3 |
| **W5** | Console migration view (backup if the projector or dashboard fails) | §3 |
| **W6** | Command window (`api` helper) | §3 |
| **H1**, **H2a–c**, **H3** | Halt server (port 9001), its fleet, its proxy on `8081` | §3b |
| **Pi** | CP0000 (SSH session or the Pi's own screen) | §3 |

In **every** PowerShell window: `cd <repo>` (and, only on a machine with a venv, `.\.venv\Scripts\Activate.ps1`).

---

## 3. Start-up (T-20 min)

**W1, main server.** Ctrl-C stops it; the same command restarts it (scene 4):
```powershell
# Hybrid server over mutual TLS for the demo fleet, reachable from the Pi; output goes to a log file.
cmd /c "python -m csms.server --host 0.0.0.0 --port 9000 --tls --mode hybrid --db logs\⟦RUN⟧.db --log logs\⟦RUN⟧_events.jsonl --fleet-profile tests\fixtures\fleet_profile_demo.json --ws-ping-interval 0 > logs\⟦RUN⟧_server.log 2>&1"
```
In **W6**:
```powershell
# Last 5 lines of the server log: expect "boot check active" and "CSMS listening — wss://0.0.0.0:9000/{station_id}".
Get-Content logs\⟦RUN⟧_server.log -Tail 5
```

**W2 and W2b, the fleet.** Both use the same experiment name plus `_legacy`, so the analysis can join them (L38):
```powershell
# 44 simulated chargers CP0001-CP0044, ML-DSA capable, charging for 40 min, quick reconnects, watching the fleet.
python -m harness.load_generator --n 44 --experiment ⟦RUN⟧ --csms-url wss://localhost:9000 --cert-dir certs --crypto-mode hybrid --pq-key-dir certs\pq --charge-for 2400 --reconnect-max-delay 5 --watch-fleet https://localhost:9000/api/fleet --watch-every 2
```
```powershell
# 4 legacy chargers CP0046-CP0049 (the profile marks them classical-only, so the migration skips them).
python -m harness.load_generator --n 4 --start-index 46 --experiment ⟦RUN⟧_legacy --csms-url wss://localhost:9000 --cert-dir certs --crypto-mode hybrid --pq-key-dir certs\pq --charge-for 2400 --reconnect-max-delay 5
```

**W3, the victim CP0045:**
```powershell
# The real CP0045 in its own window, so scene 5 can stop it and start an impostor in its place.
python -m agent.station --station-id CP0045 --csms-url wss://localhost:9000 --cert-dir certs --pq-key-dir certs\pq --crypto-mode hybrid --charge-for 2400 --reconnect-max-delay 5
```

**Pi** (Pi minder, venv active, in ⟦PI-REPO⟧):
```bash
# The Pi as charger CP0000: GPIO power (LED + INA219), its own key in certs/pq, hybrid, 5 s reconnect ceiling.
python -m agent.station --station-id CP0000 --csms-url wss://⟦LAPTOP-IP⟧:9000 --cert-dir certs --power gpio --pq-key-dir certs/pq --crypto-mode hybrid --supported-algorithms ECDSA-P256,ML-DSA-44 --charge-for 3600 --reconnect-max-delay 5
```
This is `scripts/pi_charger.sh` plus `--reconnect-max-delay 5`; without it, the Pi can take about 30 s to come back in scene 4. The LED lights when the Pi starts charging.

**No Pi yet (software rehearsal, tracker step 6b/6d)** — run a simulated stand-in on the laptop instead, in its own window:
```powershell
# Simulated CP0000 (same id and certificate as the Pi, simulated power) so the fleet and the big Pi tile are complete.
python -m agent.station --station-id CP0000 --csms-url wss://localhost:9000 --cert-dir certs --pq-key-dir certs\pq --crypto-mode hybrid --charge-for 2400 --reconnect-max-delay 5
```
Its tile says "measured: on the Pi's console" (no INA219). **Before the real Pi joins, reset the state (§1c):** the stand-in enrols its own key, and a server that still holds it would cut off the real Pi with 1008.

**W4, dashboard proxy (main):**
```powershell
# Serves the dashboard on http://localhost:8080/dashboard/ and passes /api to the TLS server (a browser has no client certificate).
python -m dashboard.serve --api https://localhost:9000 --cert-dir certs --port 8080
```

**W5, console view:**
```powershell
# Live console view of the migration; exits by itself when the migration it watched ends, so restart it before scene 7.
python -m harness.watch_migration --url https://localhost:9000 --cert-dir certs
```

**W6, command helper:**
```powershell
# Defines `api`: a GET to the server with a CA-signed client certificate (L08: any station certificate opens /api).
# Python, not curl.exe: Windows curl.exe (Schannel) cannot load our PEM client certificates.
function api($p) { $env:API_PATH = $p; python -c "import os, urllib.request, ssl; c=ssl.create_default_context(cafile='certs/root.pem'); c.load_cert_chain(certfile='certs/CP0049.crt.pem', keyfile='certs/CP0049.key.pem'); r=urllib.request.urlopen('https://localhost:9000'+os.environ['API_PATH'], context=c, timeout=10); print(r.read().decode())" }
# Server settings: expect "tls": true, "identity_check": "enforce", "boot_check": true, "pq_algorithm": "ML-DSA-44".
api "/api/health"
```

**Ready when** tab 8080 shows **50 connected, 50 charging** (500 with §3c), and the Pi tile (CP0000, three columns wide) shows "measured: NN mW". The header must say Mode hybrid, TLS 🔒 TLSv1.3, Identity check enforce, Boot key check on, and must not show a **MOCK** badge.

### 3b. The halt server (H1–H3); start it before the panel arrives

This is a **second** fleet on port 9001, with Track A's refusers CP0021–25 in the middle (Track B's profile `fleet_profile_halt_n50.json`). It has its **own key folder** (`certs\pq_halt`), because its chargers reuse the ids CP0001–CP0050. Both of its load generators use the experiment name `halt_⟦RUN⟧` (L38).

**H1:**
```powershell
# Second hybrid server on port 9001 with the halt profile (refusers CP0021-25 claim ML-DSA but refuse the key).
cmd /c "python -m csms.server --port 9001 --tls --mode hybrid --db logs\⟦RUN⟧_halt.db --log logs\⟦RUN⟧_halt_events.jsonl --fleet-profile tests\fixtures\fleet_profile_halt_n50.json --ws-ping-interval 0 > logs\⟦RUN⟧_halt_server.log 2>&1"
```
**H2a, H2b, H2c** (one window each):
```powershell
# Halt fleet part 1: CP0001-CP0020.
python -m harness.load_generator --n 20 --experiment halt_⟦RUN⟧ --csms-url wss://localhost:9001 --cert-dir certs --crypto-mode hybrid --pq-key-dir certs\pq_halt --charge-for 2400
```
```powershell
# Track A's 5 refusers CP0021-CP0025 over TLS: they answer the enrolment request with UnknownVendorId.
python -m tests.fixtures.fake_station CP0021 CP0022 CP0023 CP0024 CP0025 --tls --cert-dir certs --url wss://localhost:9001 --charge-for 0
```
```powershell
# Halt fleet part 2: CP0026-CP0050 (same experiment name, so it is analysed as one run with part 1).
python -m harness.load_generator --n 25 --start-index 26 --experiment halt_⟦RUN⟧ --csms-url wss://localhost:9001 --cert-dir certs --crypto-mode hybrid --pq-key-dir certs\pq_halt --charge-for 2400
```
**H3, its dashboard proxy:**
```powershell
# Dashboard for the halt server on http://localhost:8081/dashboard/.
python -m dashboard.serve --api https://localhost:9001 --cert-dir certs --port 8081
```
Tab 8081 should show 50 connected.

### 3c. The 500-charger layout (499 simulated + the Pi) — tracker steps 6c/6d

Same windows as §3, with **three** changes. Everything else (W3 victim CP0045, the Pi or its stand-in, W4, W5, W6, the halt server §3b) is unchanged.

**W1** — the 500-charger profile (CP0000 + CP0001–CP0449 capable, CP0450–CP0499 legacy):
```powershell
# Hybrid server over mutual TLS for the 500-charger demo fleet; output goes to a log file.
cmd /c "python -m csms.server --host 0.0.0.0 --port 9000 --tls --mode hybrid --db logs\⟦RUN⟧.db --log logs\⟦RUN⟧_events.jsonl --fleet-profile tests\fixtures\fleet_profile_demo_n500.json --ws-ping-interval 0 > logs\⟦RUN⟧_server.log 2>&1"
```
**W2** — CP0001–CP0044 (no `--watch-fleet` at this size: 500-charger snapshots every 2 s would make a diary of hundreds of MB, L33):
```powershell
# 44 simulated chargers CP0001-CP0044, charging for 40 min, quick reconnects.
python -m harness.load_generator --n 44 --experiment ⟦RUN⟧ --csms-url wss://localhost:9000 --cert-dir certs --crypto-mode hybrid --pq-key-dir certs\pq --charge-for 2400 --reconnect-max-delay 5
```
**W2b** — CP0046–CP0499 (CP0450–0499 are the legacy chargers), with the laptop's CPU and lag recorded:
```powershell
# 454 simulated chargers CP0046-CP0499, staggered start, and machine readings (CPU, memory, loop lag) in the diary.
python -m harness.load_generator --n 454 --start-index 46 --experiment ⟦RUN⟧_b --csms-url wss://localhost:9000 --cert-dir certs --crypto-mode hybrid --pq-key-dir certs\pq --charge-for 2400 --reconnect-max-delay 5 --watch-machine
```
The two load generators share the experiment name (`⟦RUN⟧` / `⟦RUN⟧_b`), so the analysis joins them into one run (L38).

**Ready when** tab 8080 shows **500 connected** (499 + the Pi or its stand-in), the fleet as small squares, and Controls pre-filled with **canary 10, wave size 50**.

**Measured on a 2-core Linux test machine (13 Oct, 499 chargers over TLS, hybrid):** all 499 connected within about a minute; the migration (449 chargers, canary 10, waves of 50) finished in **about 5 s**; a server restart (scene 4) had all 499 back and boot-checked within **about 10 s**; a key rotation took **about 10 s**. **Not yet measured on the Windows demo laptop: that is step 6d.**

**After the 500 rehearsal, check the laptop was not saturated:**
```powershell
# Analyse the 500-charger run (main server log + both diaries); its summary line shows machine CPU p95 and loop lag p95, and "SATURATED" if over the limits.
python -m analysis.run --events logs\⟦RUN⟧_events.jsonl --harness-log logs\⟦RUN⟧_n44_hybrid.jsonl --harness-log logs\⟦RUN⟧_b_n454_hybrid.jsonl --out analysis\output_⟦RUN⟧
```
**Saturated, or the dashboard lags behind → use the 50-charger layout (§3) for the demo.**

---

## 4. The scenes

For each scene: **Do**, then **See**, then **Say**, then **If not**.

### Scene 1 — Fleet up (tab 8080)
- **Do:** click the Pi tile (CP0000), then any other tile.
- **See:**
  - The inspector shows TLS 1.3 + cipher, "identity ✓" (certificate CN = station id) and "no post-quantum key yet".
  - The Pi tile shows "LED 100 % · 7.4 kW (scaled)" and its real INA219 reading in mW.
  - The header shows TLS on, `enforce`, ML-DSA-44, boot key check on.
- **Say:** "50 chargers, one of them real hardware, all on mutual TLS. Classical today."
- **If not:**
  - The Pi is missing: the Pi minder restarts the Pi command (§3) and checks the hotspot IP and the `curl` from §1b.
  - The cipher is blank, or the page will not load: you are on `:9000/dashboard/` directly. Use the proxy tab `:8080`.

### Scene 2 — Migration to hybrid (tab 8080)
- **Do:** Start migration (canary **5**, wave **10**; at 500 chargers the dashboard pre-fills canary **10**, wave **50**).
- **At 500:** the whole migration takes only a few seconds, so **say the "Say" line first, then press the button**; for a slower sweep use wave size **25**.
- **See:**
  - The Pi is in the canary (CP0000 sorts first).
  - Tiles turn amber, then green, wave by wave.
  - The ticker shows "🔑 CP00xx made its own ML-DSA-44 key · key `xxxx…` (private key never left the charger)", then "✓ CP00xx ML-DSA-44 signature verified · NN ms (migration)".
  - CP0046–49 stay dark (incompatible).
  - **The power line does not dip.**
  - W5 shows the same waves, then exits with a summary.
- **Say:** "Each charger makes its own key on the spot. Only the public half travels. It must prove it holds the private half before it counts as migrated."
- **If not:** a 409 means a run is already going; wait for it. As a fallback, in W6:
  ```powershell
  # Start the same migration from the command line: canary 5, waves of 10, target hybrid.
  api "/api/migration/start?wave_size=10&canary_count=5&target_mode=hybrid"
  ```

### Scene 3 — Halt (switch to tab 8081)
- **Do:** Start migration (canary 5, wave 10).
- **See:**
  - The canary and wave 1 go green.
  - Wave 2 (CP0016–25) has 5 refusers, so the **wave is rolled back** (red).
  - The phase is **rolled_back**: the migration **stops**; CP0026–50 stay pending. Expected: 15 migrated, 10 rolled back, 25 pending.
- **Say:** "Too many failures in one wave: we put that wave back and stop. Nothing beyond it is touched."
- **Then:** switch back to tab 8080.

### Scene 4 — Reconnect storm (main)
- **Do:** W1 **Ctrl-C**, count to 10 out loud, then run the **same W1 command** again.
- **See:**
  - Tiles go grey.
  - Within about 10 s they come back, and the ticker fills with "✓ … (boot)" for every migrated charger, the Pi included.
  - "Recovered" climbs back to 50.
- **Say:** "After the restart, every migrated charger had to prove its key again before it counted as recovered."
- **If not:**
  - Some chargers stay grey for more than 30 s: they are in back-off; wait, they retry.
  - Many are closed with `pq_auth_failed`: the key folder and `--db` don't match. Rehearsal only: reset (§5).

### Scene 5 — Attack: stolen certificate (W3)
- **Do:**
  1. W3 **Ctrl-C**: the real CP0045 goes offline.
  2. Start the impostor in W3: same id and certificate, **no key**:
     ```powershell
     # Impostor: CP0045's genuine TLS certificate but no ML-DSA key; retries every 1-2 s.
     python -m agent.station --station-id CP0045 --csms-url wss://localhost:9000 --cert-dir certs --pq-key-dir none --crypto-mode hybrid --reconnect-base-delay 1 --reconnect-max-delay 2
     ```
- **See:**
  - The ticker shows "✗ CP0045 key check FAILED" and a **red flash**: "⛔ CP0045 cut off (code 1008)", repeating every second or two.
  - The W3 log shows `connection closed: code=1008 reason='pq_auth_failed'`.
- **Say:** "A perfectly valid classical certificate, stolen. Before migration this would have worked. Now it can't answer the post-quantum challenge, so it's cut off, every time."
- **Then:** **Ctrl-C** the impostor and run the first W3 command again (the real CP0045). The ticker shows its boot check ✓.
- **Extra (optional), the wrong-identity attack** (`experiments/e5_attack.md` (b)):
  ```powershell
  # CP0004's genuine certificate used as CP0003: refused by the identity check before it ever boots.
  python -m agent.station --station-id CP0003 --csms-url wss://localhost:9000 --cert certs\CP0004.crt.pem --key certs\CP0004.key.pem --ca certs\root.pem --pq-key-dir none --crypto-mode hybrid --reconnect-max-attempts 1
  ```

### Scene 6 — Curtailment (tab 8080 + the physical LED)
- **Do:** move the power-limit slider to **3.7 kW** and press Apply, then press **Clear**.
- **See:**
  - The fleet power chart drops to about half, then recovers.
  - **The Pi's LED dims**, then brightens again.
  - The Pi tile's "measured: NN mW" drops with it (L37), and the Pi console shows the same.
- **Say:** "One command from the operator changes physical power across the fleet. That's exactly why the identity checks matter."
- **If not:** use W6:
  ```powershell
  # Limit every connected charger to 3.7 kW.
  api "/api/fleet/limit?watts=3700"
  # Remove the limit again.
  api "/api/fleet/clear-limit"
  ```

### Scene 7 — Key rotation (tab 8080)
- **Do:**
  1. **First restart W5** (the W5 command in §3); it exited after scene 2. Open it **before** pressing the button (L41).
  2. Press Rotate keys (canary 5, wave 10; at 500: canary 10, wave 50).
- **See:**
  - Tiles (or squares) turn **purple** wave by wave as each charger proves its new key; the legend counts "new key (rotated)", and the wave rows fill purple.
  - The ticker shows "↻ CP00xx key rotated · `aaaa…` → `bbbb…` · no disconnect" per charger.
  - The inspector's `pq_key_id` changes.
  - W5 turns letters into **N** (new key).
  - **No tile goes grey**, and the power line is flat.
- **Say:** "Every charger gets a new key while the old one stays valid until the new one is proven. Nobody disconnects."
- **If not:**
  - 409: a migration is still finishing; wait.
  - Fallback in W6:
    ```powershell
    # Start the key rotation from the command line: canary 5, waves of 10.
    api "/api/migration/rotate?wave_size=10&canary_count=5"
    ```

### Scene 8 — Results page
- **Do:** open `analysis\output\report.html` (built in §1a).
- **Say:** the headline line of each experiment at the top of the page:
  - E1 and E2: classical vs hybrid;
  - E3: N = 100, the halt, the rotation;
  - E4: certificate sizes vs the OCPP limits;
  - E5: attacks refused;
  - S1: 500 chargers on one laptop.

---

## 5. Reset between rehearsals (5 min)

1. Ctrl-C everything: W2, W2b, W3, W4, W5, H1–H3, then W1; the Pi too.
2. Archive the keys (as in §1c):
   ```powershell
   # Archive the main fleet's and the halt fleet's charger keys.
   move certs\pq certs\pq_old_⟦time⟧
   move certs\pq_halt certs\pq_halt_old_⟦time⟧
   ```
   ```bash
   # On the Pi: archive its key.
   mv certs/pq certs/pq_old_⟦time⟧
   ```
3. Pick a **new** ⟦RUN⟧ name (new `--db` and log names).
4. Start again at §3. **Don't** re-run `bootstrap_pki`: the Pi's certificates would no longer match.

---

## 6. After the demo

- **The demo's logs are not experiment evidence.** Two servers ran the same charger ids at the same time, so the analysis cannot tell their lines apart. The evidence is the separate runs of tracker step 13. Keep `logs\⟦RUN⟧_events.jsonl` and the tester diaries only as a record.
- **Never** share `*_server.log`, `*.db` or anything in `certs\`.
- If the panel asks for numbers on the attacks:
  ```powershell
  # Prints the three E5 verdicts (stolen certificate / wrong identity / curtailment) from the main server's log.
  python -m experiments.e5_check logs\⟦RUN⟧_events.jsonl
  ```

---

## 7. Quick fixes

| Symptom | Fix |
|---|---|
| Dashboard blank or shows **MOCK** | Wrong tab, or the proxy is not running: W4, then open `http://localhost:8080/dashboard/` |
| Port 9000 (or 9001) already in use | An old server is still running: close its window. `Get-Process python \| Stop-Process` stops **every** Python process |
| Pi: `certificate verify failed` | The Pi has certificates from an older `bootstrap_pki`: copy the three files again (§1b) |
| Pi can't reach the laptop | Same hotspot? Firewall rule (§1a)? From the Pi: `ping ⟦LAPTOP-IP⟧` |
| Pi tile shows "measured: on the Pi's console" | The Pi is running without `--power gpio`, or the INA219 gives no reading: run `python -m agent.gpio_power` on the Pi |
| Server refuses to start: "--mode hybrid needs post-quantum migration" | The 1312 check fails on this laptop (quantcrypt engine): fix it with the quantcrypt steps in the dev plans |
| Everyone closed with `pq_auth_failed` after a restart | Key folder and `--db` don't match: reset both (§5) |
| W5 shows "waiting for a NEW migration" | Normal after a migration ended: it waits for the next one (scene 7) |
| `api` prints nothing / curl errors about the certificate | Use the Python `api` helper (§3 W6): Windows `curl.exe` cannot load PEM client certificates |
| 500 chargers: dashboard updates lag, or Task Manager shows CPU near 100 % | Fall back to the 50-charger layout (§3): stop everything, reset (§5), start §3 |
| Projector or dashboard dies | Narrate from **W5** (console migration view) and W6 `api "/api/fleet"` |

---

*Checked for v1.0 (2026-10-12, Track C, against `main` = `034bcb4`):*

- *every command's flags against the programs' `--help`;*
- *the server's log lines ("boot check active", "CSMS listening");*
- *the Pi command against `scripts/pi_charger.sh`, and the Pi install against `scripts/pi_setup.sh`;*
- *the expected halt numbers against Track B's profile.*

*Track A's 2026-10-11 checks still stand:*

- *a server restart with the same `--db` restores the keys;*
- *with `--reconnect-max-delay 5`, chargers are back and boot-checked within about 6 s;*
- *the scene 5 and 6 commands work as in `experiments/e5_attack.md`.*

**Not yet run end to end:** the full 50-charger layout with the halt server and the Pi. That is the first dress rehearsal (tracker step 17).
