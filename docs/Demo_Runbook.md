# PQCharge — Demo Runbook (A-F8, tracker step 4)

**v1.2, 2026-10-14** (v1.1: 2026-10-13, v1.0: 2026-10-12). Written by Track C (Koshambi) for Track A's task A-F8, from the v0.1 draft (Track A, 2026-10-11). Lead on the day: Bhargavi.

**What changed in v1.2:**

- **The demo is now 8 variants (V1–V8), shown one by one** (§4), instead of 8 scenes:
  - V1 normal charging (44 + 4 legacy + CP0000);
  - V2 migration;
  - V3 the **attack charger CP0045**, started separately while the others charge;
  - V4 storm, V5 rotation, V6 halt, V7 curtailment, V8 results.
- **CP0045 is no longer started with the fleet.** It is the attack charger of V3, in its own window (W3).
- **New dashboard** (same address, same proxy). It is drawn as an animation:
  - it starts **empty** ("No chargers connected");
  - a **tile pops in** for every charger that connects (any number: 20, 50 or 500 — nothing hard-coded);
  - tiles go **green** while charging, **blue** while migrating, **purple + 🛡** once post-quantum, **burgundy** when rolled back, **grey hatched** for legacy, **red** when refused;
  - canary chargers carry a gold **C**, the others **W1**, **W2**, … (their wave);
  - a **summary card** appears when a migration ends;
  - a **"Migration, live"** chart shows how many chargers are in each colour, second by second.
- **The server now shows refused TLS handshakes** (forged, old or missing certificate). Before, they left no trace. Each one is a `connection_failed` line, and a red **"?" unknown device** tile on the dashboard. (Track A file `csms/server.py`, changed with the team's OK.)
- **New helper `experiments/forge_cert.py`:** it makes a forged CP0045 certificate (signed by a home-made CA, not ours) for V3.
- **The Pi's LED blinks for ~3 s while the Pi is being migrated** (and when its key is rotated). It is on while charging, as before. (Track B file `agent/gpio_power.py`; the trigger is in `agent/station.py`.)

**What changed in v1.1 (tracker steps 6–6c):**

- Every load generator says `--crypto-mode hybrid`.
- Two fleet sizes: 50 (§3) and 500 (§3c).
- The `api` helper uses Python, not `curl.exe`.
- There is a simulated stand-in for the Pi.
- Certificates are made for up to 500 chargers.
- Koshambi's laptop runs global Python 3.13 (no venv).

> **Fill in these blanks before the first rehearsal** (they are marked **⟦like this⟧**):
> - **Demo laptop:** Koshambi's Windows laptop (decided). Commands are **PowerShell**. On a Mac, use `/` for `\` and skip the firewall step.
> - ⟦LAPTOP-IP⟧: the laptop's IP on the phone hotspot (iPhone hotspot, 13 Oct: **172.20.10.6** — check with `ipconfig` each day; if it changed, redo the certificates and copy them to the Pi again).
> - ⟦PI-USER@PI⟧: how we SSH into the Pi (e.g. `pi@raspberrypi.local`).
> - ⟦PI-REPO⟧: the repo folder on the Pi (e.g. `~/PQCharge`).
> - ⟦RUN⟧: a new name per rehearsal (`demo1`, `demo2`, `final`).
> - ⟦time⟧: any timestamp for archive folder names (e.g. `1830`).

---

## 0. The variants (≈ 14 min) and who does what

| # | Variant | ~min | Shown on | Proves |
|---|---|---|---|---|
| V1 | **Normal charging:** 44 chargers + 4 legacy + CP0000 (Pi) connect and charge | 2 | Dashboard (tab 8080) | Classical baseline over mutual TLS; real hardware in the fleet |
| V2 | **Migration** to hybrid, controlled from the browser: canary → waves; summary card | 2 | Dashboard + W5 + the Pi's LED | Live migration, charging never stops (core claim) |
| V3 | **Attack charger CP0045:** forged certificate, wrong identity, then the real CP0045 migrates, then a stolen certificate without the key | 3 | Dashboard + W3 | Robustness: every kind of impostor is refused |
| V4 | Reconnect storm: every charger re-proves its key | 1.5 | Dashboard | Hybrid = a real check at every boot |
| V5 | Key rotation without disconnection | 1.5 | Dashboard + W5 | Novelty claim |
| V6 | Halt: a bad wave is rolled back (burgundy), the migration stops | 1.5 | Dashboard (tab 8081) | Safety: threshold, rollback, halt |
| V7 | Curtailment: the Pi's LED dims, its mW drop | 1 | Dashboard + the LED | Cyber-physical loop |
| V8 | Results page | 1.5 | `report.html` | The numbers |

- **Driver** (types commands): Bhargavi.
- **Narrator** (talks to the panel): ⟦name⟧.
- **Pi minder** (watches the Pi console and the LED): ⟦name⟧.
- **Fleet size:** 50 (§3) or 500 (§3c), decided by tracker step 6d. Every variant works at both sizes. V6 always uses its own 50-charger fleet.
- **The order matters:**
  - V3 needs V2 done first (an attacker without the key is only refused once the real CP0045 has a key);
  - V5 needs a migrated fleet;
  - V6 can be shown at any point (it is a separate server).

### The colours (say this once, in V1)

| Tile | Means |
|---|---|
| empty panel with a radar | no charger connected yet |
| slate | connected, not charging |
| **green**, moving light, ⚡ | charging, classical identity |
| **blue**, pulsing, ⟳ | being migrated right now |
| **purple + 🛡** (glows while charging) | post-quantum hybrid: has its own ML-DSA-44 key and proved it |
| **burgundy**, ↩ | its wave was rolled back |
| **grey, hatched** | legacy charger (no ML-DSA-44): skipped, never touched |
| **dim, dashed**, ⏻ | offline |
| **red**, shaking, ⛔ | refused just now (attack). Also a red banner on top, and a ticker line |
| red **"?"** tile | an unknown device was refused during TLS (no charger id is known then) |
| gold **C** / **W1, W2, …** | in the canary group / in that wave |
| **L** (before the migration) | legacy charger |
| 🔒 / ✓ | TLS on this connection / post-quantum key verified on this connection |

**About the speed.** The real server migrates a wave in a fraction of a second, far faster than the page refreshes (once a second). So the page **replays the waves in order**, each one blue for at least 1.2 s. The phase line says "Replaying the waves: the server already finished". The summary card shows the server's **real** time ("Server time", e.g. 0.8 s). Say so if asked: the replay is only for the eye; no number is changed.

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

**Certificates.** This makes a **new** root CA and **deletes the previous run's certificate files** in `certs\` first (since 2026-10-14; `certs\pq` and other folders are kept). So run it **once**, always with `--also CP0000`, and copy to the Pi afterwards:
```powershell
# Certificates for CP0001-CP0500 (both fleet sizes and the N = 100 experiment runs) + the Pi (CP0000); the server certificate also covers the hotspot IP.
python -m experiments.bootstrap_pki --count 500 --also CP0000 --server-san ⟦LAPTOP-IP⟧
```

**The forged certificate for V3** (once, after the line above; it lands in `certs\attacker\`, which is never committed):
```powershell
# A CP0045 certificate signed by a home-made "attacker" CA, not by our root CA.
python -m experiments.forge_cert --station-id CP0045
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

**Browser:** two tabs, `http://localhost:8080/dashboard/` (main) and `http://localhost:8081/dashboard/` (halt). Full screen (F11), zoom about 110–125 %.

**Rehearse the look without any server** (optional, any time):
```powershell
# A pretend fleet (MOCK badge): chargers connect one by one over 8 s, then charge. Buttons "Mock: ..." play the attacks.
python -m dashboard.serve --mock
```

### 1b. Raspberry Pi (Pi minder; wiring done by the team)

On the Pi, from ⟦PI-REPO⟧:
```bash
# One-time install: system GPIO packages, I2C on, venv, requirements, INA219 library, quantcrypt build, 1312 check, crypto tests.
bash scripts/pi_setup.sh
# Turn on the venv (do this in every new Pi terminal).
source .venv/bin/activate
# Hardware self-test without a server: LED on/dim/off and INA219 readings.
python -m agent.gpio_power
# The LED blink the Pi shows while it is being migrated (V2): blinks ~3 s, then the LED is off again.
python -c "import time; from agent.gpio_power import GPIOPower; p = GPIOPower(7400.0); p.indicate_migration(); time.sleep(3.5)"
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

**Where the LED logic lives** (for questions):
- `agent/gpio_power.py`: the LED itself.
  - `close_contactor()` → on;
  - `set_power_limit()` → dimmed;
  - `open_contactor()` → off;
  - `indicate_migration()` → blinks for 3 s (`MIGRATION_BLINK_S`), then shows the above again.
- `agent/station.py`: `handle_pq_enrolment()` calls `indicate_migration()` after the Pi made its new key. That message comes only from a migration or a key rotation, never from a boot check. Simulated chargers have no LED, so nothing happens there.

---

## 1c. Before every rehearsal and the real demo (fresh state)

The chargers' key folders and the server's `--db` file describe the **same** keys. Reset them **together**. Otherwise the server (correctly) refuses chargers whose keys it doesn't know, and that looks like an attack (this is what happened in the 6b `sw1` session).

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
| **W4** | Dashboard proxy → `localhost:8080` (main) | §3 |
| **W2** / **W2b** | Main fleet CP0001–CP0044 / legacy CP0046–CP0049 | V1 |
| **Pi** (or **W7** stand-in) | CP0000 | V1 |
| **W3** | The **attack charger CP0045** (forged, wrong identity, real, impostor) | V3 |
| **W5** | Console migration view (backup if the projector or dashboard fails) | §3 |
| **W6** | Command window (`api` helper) | §3 |
| **H1**, **H2a–c**, **H3** | Halt server (port 9001), its fleet, its proxy on `8081` | §3b |

In **every** PowerShell window: `cd <repo>` (and, only on a machine with a venv, `.\.venv\Scripts\Activate.ps1`).

---

## 3. Start-up (T-20 min): server and dashboard only, NO chargers yet

**W1, main server.** Ctrl-C stops it; the same command restarts it (V4):
```powershell
# Hybrid server over mutual TLS for the demo fleet, reachable from the Pi; output goes to a log file.
cmd /c "python -m csms.server --host 0.0.0.0 --port 9000 --tls --mode hybrid --db logs\⟦RUN⟧.db --log logs\⟦RUN⟧_events.jsonl --fleet-profile tests\fixtures\fleet_profile_demo.json --ws-ping-interval 0 > logs\⟦RUN⟧_server.log 2>&1"
```
In **W6**:
```powershell
# Last 5 lines of the server log: expect "boot check active" and "CSMS listening — wss://0.0.0.0:9000/{station_id}".
Get-Content logs\⟦RUN⟧_server.log -Tail 5
```

**W4, dashboard proxy (main):**
```powershell
# Serves the dashboard on http://localhost:8080/dashboard/ and passes /api to the TLS server (a browser has no client certificate).
python -m dashboard.serve --api https://localhost:9000 --cert-dir certs --port 8080
```
Open tab 8080 now. **It must show the empty panel "No chargers connected"**, the header with Mode hybrid, TLS 🔒, Identity check enforce, Boot key check on, and **no MOCK badge**.

**W5, console view:**
```powershell
# Live console view of the migration; exits by itself when the migration it watched ends, so restart it before V5.
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
Tab 8081 should show 50 green tiles (`--charge-for 0` means the refusers charge until stopped).

### 3c. The 500-charger layout (499 simulated + the Pi) — tracker steps 6c/6d

Same windows and variants. **W1** uses the 500 profile, and V1 starts **W2b** with 454 chargers instead of 4. CP0045 stays free for V3 in both layouts.

**W1** — the 500-charger profile (CP0000 + CP0001–CP0449 capable, CP0450–CP0499 legacy):
```powershell
# Hybrid server over mutual TLS for the 500-charger demo fleet; output goes to a log file.
cmd /c "python -m csms.server --host 0.0.0.0 --port 9000 --tls --mode hybrid --db logs\⟦RUN⟧.db --log logs\⟦RUN⟧_events.jsonl --fleet-profile tests\fixtures\fleet_profile_demo_n500.json --ws-ping-interval 0 > logs\⟦RUN⟧_server.log 2>&1"
```
**W2** (V1) — CP0001–CP0044 (no `--watch-fleet` at this size: 500-charger snapshots every 2 s would make a diary of hundreds of MB, L33):
```powershell
# 44 simulated chargers CP0001-CP0044, charging for 40 min, quick reconnects.
python -m harness.load_generator --n 44 --experiment ⟦RUN⟧ --csms-url wss://localhost:9000 --cert-dir certs --crypto-mode hybrid --pq-key-dir certs\pq --charge-for 2400 --reconnect-max-delay 5
```
**W2b** (V1) — CP0046–CP0499 (CP0450–0499 are the legacy chargers), with the laptop's CPU and lag recorded:
```powershell
# 454 simulated chargers CP0046-CP0499, staggered start, and machine readings (CPU, memory, loop lag) in the diary.
python -m harness.load_generator --n 454 --start-index 46 --experiment ⟦RUN⟧_b --csms-url wss://localhost:9000 --cert-dir certs --crypto-mode hybrid --pq-key-dir certs\pq --charge-for 2400 --reconnect-max-delay 5 --watch-machine
```
Above 100 chargers the dashboard draws **small squares** (one per charger) and pre-fills **canary 10, wave 50**. Gold border = canary; white dot = key verified.

**Measured on a 2-core Linux test machine (13 Oct, 499 chargers over TLS, hybrid):**
- all 499 connected within about a minute;
- the migration (449 chargers, canary 10, waves of 50) took **about 5 s**;
- after a server restart (V4), all 499 were back and boot-checked within **about 10 s**;
- a key rotation took **about 10 s**.

**Not yet measured on the Windows demo laptop: that is step 6d.**

**After the 500 rehearsal, check the laptop was not saturated:**
```powershell
# Analyse the 500-charger run (main server log + both diaries); its summary line shows machine CPU p95 and loop lag p95, and "SATURATED" if over the limits.
python -m analysis.run --events logs\⟦RUN⟧_events.jsonl --harness-log logs\⟦RUN⟧_n44_hybrid.jsonl --harness-log logs\⟦RUN⟧_b_n454_hybrid.jsonl --out analysis\output_⟦RUN⟧
```
**Saturated, or the dashboard lags behind → use the 50-charger layout (§3) for the demo.**

---

## 4. The variants

For each variant: **Do**, then **See**, then **Say**, then **If not**.

### V1 — Normal charging: 44 chargers + 4 legacy + CP0000 (tab 8080)

- **Do** (the panel is watching the empty dashboard):
  1. **W2**:
     ```powershell
     # 44 simulated chargers CP0001-CP0044, ML-DSA capable, charging for 40 min, quick reconnects, watching the fleet.
     python -m harness.load_generator --n 44 --experiment ⟦RUN⟧ --csms-url wss://localhost:9000 --cert-dir certs --crypto-mode hybrid --pq-key-dir certs\pq --charge-for 2400 --reconnect-max-delay 5 --watch-fleet https://localhost:9000/api/fleet --watch-every 2
     ```
  2. **W2b** (same experiment name plus `_legacy`, so the analysis can join them, L38):
     ```powershell
     # 4 legacy chargers CP0046-CP0049 (the profile marks them classical-only, so the migration skips them).
     python -m harness.load_generator --n 4 --start-index 46 --experiment ⟦RUN⟧_legacy --csms-url wss://localhost:9000 --cert-dir certs --crypto-mode hybrid --pq-key-dir certs\pq --charge-for 2400 --reconnect-max-delay 5
     ```
  3. **CP0000.** Either **the Pi** (Pi minder, venv active, in ⟦PI-REPO⟧):
     ```bash
     # The Pi as charger CP0000: GPIO power (LED + INA219), its own key in certs/pq, hybrid, 5 s reconnect ceiling.
     python -m agent.station --station-id CP0000 --csms-url wss://⟦LAPTOP-IP⟧:9000 --cert-dir certs --power gpio --pq-key-dir certs/pq --crypto-mode hybrid --supported-algorithms ECDSA-P256,ML-DSA-44 --charge-for 3600 --reconnect-max-delay 5
     ```
     or, **no Pi yet**, the simulated stand-in on the laptop (**W7**):
     ```powershell
     # Simulated CP0000 (same id and certificate as the Pi, simulated power) so the fleet and the big Pi tile are complete.
     python -m agent.station --station-id CP0000 --csms-url wss://localhost:9000 --cert-dir certs --pq-key-dir certs\pq --crypto-mode hybrid --charge-for 2400 --reconnect-max-delay 5
     ```
     Before the real Pi joins a fleet the stand-in was in, reset the state (§1c): the stand-in enrolled its own key.
  4. Click the Pi tile (CP0000), then any other tile, then a legacy tile (CP0046).
- **See:**
  - The empty panel disappears; **tiles pop in** as chargers connect. The ticker says "🔌 N chargers connected (TLS + boot accepted)".
  - About a second later each tile turns **green** with moving light ("⚡ N chargers started charging").
  - The stepper moves from **Connect** to **Charge**.
  - The header counts **49 connected, 49 charging** (CP0045 is not started yet; it is V3's attack charger).
  - CP0046–49 carry an **L** (legacy: no ML-DSA-44).
  - The Pi's big tile: "LED 100 % · 7.4 kW (scaled)" and its INA219 reading in mW. The physical **LED is on**.
  - The inspector shows TLS 1.3 + cipher, "identity ✓" (certificate name = charger id), and no post-quantum key yet. For CP0046, "Can do: ECDSA-P256" only.
  - The "Migration, live" chart fills **green**, and the fleet power chart steps up to about 360 kW.
- **Say:** "Forty-nine chargers, one of them real hardware, all on mutual TLS. Four of them are older models that can't do post-quantum. All classical today, all charging."
- **If not:**
  - The Pi is missing: the Pi minder restarts the Pi command and checks the hotspot IP and the `curl` from §1b.
  - The cipher is blank, or the page will not load: you are on `:9000/dashboard/` directly. Use the proxy tab `:8080`.
  - Tiles appear but stay **slate**: they are connected but not charging yet; wait 2–3 s.

### V2 — Migration, controlled from the browser (tab 8080 + W5 + the LED)

- **Do:**
  1. In **Controls**, set **Canary 5**, **Wave size 10** (at 500: 10 and 50, pre-filled).
  2. Say the "Say" line, **then** press **Start migration → hybrid**.
  3. When the summary card appears, point at it.
- **See:**
  - The stepper moves **Charge → Canary → Waves → Hybrid**.
  - Canary tiles (gold **C**; the Pi is one of them, CP0000 sorts first) turn **blue**, then **purple + 🛡**. Then W1, W2, … wave by wave.
  - **The Pi's LED blinks for ~3 s** (its key is being made), then is on again. The Pi tile says "LED blinking (being migrated)".
  - The ticker: "🔑 CP00xx made its own ML-DSA-44 key · key `xxxx…` (private key never left the charger)", then "✓ CP00xx ML-DSA-44 signature verified · NN ms (migration)".
  - CP0046–49 turn **grey, hatched** (legacy, skipped).
  - "Migration, live": green turns into blue, then into purple. **The fleet power line does not dip.**
  - **The summary card:**
    - "✅ Migration complete";
    - **45/46 hybrid**, **4 legacy skipped**, 0 rolled back;
    - **1 not attempted** = CP0045, which is not connected yet; it is "deferred, not a failure";
    - the waves, the **server's real time** (well under a second);
    - key checks passed and their median in ms;
    - "Charging 49 → 49 (charging never stopped)".
  - W5 shows the same waves, then exits with a summary.
- **Say:** "Each charger makes its own key on the spot. Only the public half travels. It must prove it holds the private half before it counts as migrated, and nobody stopped charging. The server did all of this in under a second; the screen slows it down so we can follow."
- **If not:**
  - A 409 means a run is already going: wait for it.
  - Fallback, in W6:
    ```powershell
    # Start the same migration from the command line: canary 5, waves of 10, target hybrid.
    api "/api/migration/start?wave_size=10&canary_count=5&target_mode=hybrid"
    ```

### V3 — The attack charger CP0045, while the others charge (W3 + tab 8080)

Every step uses **W3** only. The other 49 keep charging the whole time. Point at the **Refused** counter in the header.

**(a) Forged (or old) certificate: refused during TLS.**
- **Do:**
  ```powershell
  # CP0045 with a certificate our CA never signed (made in §1a by forge_cert); it trusts our server, the server must not trust it.
  python -m agent.station --station-id CP0045 --csms-url wss://localhost:9000 --cert certs\attacker\CP0045.crt.pem --key certs\attacker\CP0045.key.pem --ca certs\root.pem --pq-key-dir none --crypto-mode hybrid --reconnect-max-attempts 1
  ```
  Optional, the same with **no certificate at all**:
  ```powershell
  # Connect to the server with no client certificate.
  python -m experiments.forge_cert --probe-no-cert --host localhost --port 9000
  ```
- **See:**
  - A **red "?" tile** ("unknown device", ×1, ×2, …) appears and shakes.
  - The red banner says "Unknown device refused at the TLS handshake — certificate not signed by our CA (forged, or from an old CA)". For the probe it says "no client certificate presented".
  - The ticker says "⛔ Unknown device refused during TLS".
  - W3 ends with a connection error.
  - Click the "?" tile: the TLS error (`CERTIFICATE_VERIFY_FAILED` / `PEER_DID_NOT_RETURN_A_CERTIFICATE`).
- **Say:** "This device claims to be charger 45, but its certificate was signed by someone else's authority. It is stopped before it can say a single word, so the server doesn't even know which charger it pretended to be: that's why it's a question mark. An expired certificate from an old CA is refused the same way."
- **If not:** no "?" tile appears, but W3 shows the error. Look in W6: `Get-Content logs\⟦RUN⟧_server.log -Tail 20 | Select-String "TLS handshake refused"`. **If that line is missing on Windows, tell Track C** (this server hook is checked on Linux only). The refusal itself still works.

**(b) Wrong identity: a real certificate, but another charger's.**
- **Do:**
  ```powershell
  # CP0004's genuine certificate used as CP0045: refused by the identity check (1008) before it ever boots.
  python -m agent.station --station-id CP0045 --csms-url wss://localhost:9000 --cert certs\CP0004.crt.pem --key certs\CP0004.key.pem --ca certs\root.pem --pq-key-dir none --crypto-mode hybrid --reconnect-max-attempts 1
  ```
- **See:**
  - The **CP0045 tile appears red** and shakes.
  - The banner says "CP0045 refused — wrong identity: it presented the certificate of CP0004".
  - The ticker: "✗ CP0045 refused: it presented the certificate of CP0004".
  - After ~8 s the tile turns dim (CP0045 is not connected) with a small **⛔1** flag.
- **Say:** "Now a real certificate from our own authority, stolen from charger 4, used to pretend to be charger 45. The name in the certificate doesn't match, so it's cut off."

**(c) The real CP0045 joins late, and is migrated.**
- **Do:**
  1. Start the real CP0045:
     ```powershell
     # The real CP0045 with its own certificate, joining a running fleet.
     python -m agent.station --station-id CP0045 --csms-url wss://localhost:9000 --cert-dir certs --pq-key-dir certs\pq --crypto-mode hybrid --charge-for 2400 --reconnect-max-delay 5
     ```
  2. When it is green, press **Start migration → hybrid** again.
- **See:**
  - The CP0045 tile turns **green**: a classical charger among purple ones.
  - After Start, CP0045 goes **blue, then purple**. The others **stay purple**.
  - The summary card now says **46/46**, 0 not attempted.
  - **The Pi's LED blinks again**, and the key-check count is about 46, not 1. This is not a fault: **a new migration run enrols EVERY capable charger again** (each gets a fresh key; Track B's orchestrator takes all chargers as candidates). Their tiles stay purple because they were already post-quantum.
- **Say:** "A charger that was offline during the migration was simply deferred, not failed. It joins later and gets upgraded on the next run."

**(d) Stolen certificate without the key: refused at every boot (1008).**
- **Do:** **Ctrl-C** the real CP0045 in W3, then:
  ```powershell
  # Impostor: CP0045's genuine TLS certificate, but no ML-DSA key; retries every 1-2 s.
  python -m agent.station --station-id CP0045 --csms-url wss://localhost:9000 --cert-dir certs --pq-key-dir none --crypto-mode hybrid --reconnect-base-delay 1 --reconnect-max-delay 2
  ```
- **See:**
  - The CP0045 tile **goes red** again and again. The banner says "failed its post-quantum key check → cut off (1008)".
  - The ticker: "✗ CP0045 key check FAILED", then "⛔ CP0045 cut off (code 1008)", every second or two.
  - **Refused** keeps counting up.
  - W3 shows `connection closed: code=1008 reason='pq_auth_failed'`.
- **Say:** "This time the certificate is perfectly valid and the name matches: it was stolen from charger 45 itself. Before the migration this would have worked. Now it can't answer the post-quantum challenge, so it's cut off, every time."

**(e) Back to normal.**
- **Do:** **Ctrl-C** the impostor, then run the (c) command again (the real CP0045).
- **See:** the ticker shows its boot check ✓; within ~8 s the tile is **purple** again.

### V4 — Reconnect storm (main)

- **Do:** W1 **Ctrl-C**, count to 10 out loud, then run the **same W1 command** again.
- **See:**
  - The page says "server not reachable", and **every tile goes dim and dashed** (offline). "Migration, live" drops to grey.
  - Within about 10 s they come back **purple** (legacy grey). The ticker fills with "✓ … (boot)" for every migrated charger, the Pi included.
  - "Recovered" climbs back to 50.
  - The server is a new run: the migration panel says IDLE, and the summary card is gone. That is normal.
- **Say:** "After the restart, every migrated charger had to prove its key again before it counted as recovered."
- **If not:**
  - Some chargers stay dim for more than 30 s: they are in back-off; wait, they retry.
  - Many are closed with `pq_auth_failed`: the key folder and `--db` don't match. Rehearsal only: reset (§5).

### V5 — Key rotation (tab 8080)

- **Do:**
  1. **First restart W5** (the W5 command in §3); it exited after V2. Open it **before** pressing the button (L41).
  2. Press **Rotate keys** (canary 5, wave 10; at 500: canary 10, wave 50).
- **See:**
  - The phase says **ROTATION**; the stepper says Rotate canary → Rotate waves → Rotated.
  - Tiles stay purple and get a **violet glowing border** wave by wave as each proves its new key. The legend counts "new key (rotated)"; the wave rows fill violet.
  - The ticker: "↻ CP00xx key rotated · `aaaa…` → `bbbb…` · no disconnect".
  - The Pi's LED blinks (it made a new key).
  - Summary card: "↻ Key rotation complete", 46 rotated, 0 kept old key.
  - **No tile goes dim**, and the power line is flat. W5 turns letters into **N** (new key).
- **Say:** "Every charger gets a new key while the old one stays valid until the new one is proven. Nobody disconnects."
- **If not:** a 409 means a migration is still finishing; wait. Fallback in W6:
  ```powershell
  # Start the key rotation from the command line: canary 5, waves of 10.
  api "/api/migration/rotate?wave_size=10&canary_count=5"
  ```

### V6 — Halt (switch to tab 8081)

- **Do:** Start migration (canary 5, wave 10).
- **See:**
  - The canary and W1 go blue, then **purple**.
  - W2 (CP0016–25) goes blue; it has 5 refusers, so the **whole wave turns burgundy ↩** and shakes. The banner says "Wave 2 rolled back — more than 20 % failed; the migration halted".
  - W3–W5 stay **green**; their rows say **not attempted**.
  - The stepper ends at **HALTED**.
  - The summary card (burgundy): "⛔ Migration halted — wave 2 rolled back"; **15/50 hybrid, 10 rolled back, 25 not attempted**; "Halted at wave 2 (5 of 10 failed)"; the per-wave bars show purple, purple, burgundy, then nothing.
  - In "Migration, live", a burgundy band appears at the "↩ rollback" line.
- **Say:** "Too many failures in one wave: we put that wave back and stop. Nothing beyond it is touched, and the waves that passed keep their new keys."
- **Then:** switch back to tab 8080.

### V7 — Curtailment (tab 8080 + the physical LED)

- **Do:** move the power-limit slider to **3.7 kW** and press **Apply limit**; then press **Clear limit**.
- **See:**
  - The fleet power chart drops to about half, then recovers.
  - **The Pi's LED dims**, then brightens again. The Pi tile's "LED 50 %" and "measured: NN mW" drop with it (L37); the Pi console shows the same.
- **Say:** "One command from the operator changes physical power across the fleet. That's exactly why the identity checks matter."
- **If not:** use W6:
  ```powershell
  # Limit every connected charger to 3.7 kW.
  api "/api/fleet/limit?watts=3700"
  # Remove the limit again.
  api "/api/fleet/clear-limit"
  ```

### V8 — Results page

- **Do:** open `analysis\output\report.html` (built in §1a).
- **Say:** the headline line of each experiment at the top of the page:
  - E1 and E2: classical vs hybrid;
  - E3: N = 100, the halt, the rotation;
  - E4: certificate sizes vs the OCPP limits;
  - E5: attacks refused;
  - S1: 500 chargers on one laptop.

---

## 5. Reset between rehearsals (5 min)

1. Ctrl-C everything: W2, W2b, W3, W7, W4, W5, H1–H3, then W1; the Pi too.
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
4. **Reload the browser tabs** (the page keeps its counters and chart until reloaded).
5. Start again at §3. **Don't** re-run `bootstrap_pki`: the Pi's certificates would no longer match. The forged certificate in `certs\attacker\` can stay.

---

## 6. After the demo

- **The demo's logs are not experiment evidence.** Two servers ran the same charger ids at the same time, so the analysis cannot tell their lines apart. The evidence is the separate runs of tracker step 13. Keep `logs\⟦RUN⟧_events.jsonl` and the tester diaries only as a record.
- **Never** share `*_server.log`, `*.db` or anything in `certs\` (including `certs\attacker\`).
- If the panel asks for numbers on the attacks:
  ```powershell
  # Prints the three E5 verdicts (stolen certificate / wrong identity / curtailment) from the main server's log.
  python -m experiments.e5_check logs\⟦RUN⟧_events.jsonl
  ```
  The forged-certificate refusals of V3 (a) are **not** one of these three verdicts (L48); the results page lists them under E5 "Connection failures" as `tls_rejected`. Or count them with:
  ```powershell
  # How many TLS handshakes the server refused (V3 a: forged / old / missing certificate).
  (Select-String -Path logs\⟦RUN⟧_events.jsonl -Pattern '"tls_rejected"').Count
  ```

---

## 7. Quick fixes

| Symptom | Fix |
|---|---|
| Dashboard blank or shows **MOCK** | Wrong tab, or the proxy is not running: W4, then open `http://localhost:8080/dashboard/` |
| Dashboard shows tiles before V1 started | Chargers from an earlier rehearsal are still running, or the page was not reloaded: stop them, reload |
| Port 9000 (or 9001) already in use | An old server is still running: close its window. `Get-Process python \| Stop-Process` stops **every** Python process |
| Pi: `certificate verify failed` | The Pi has certificates from an older `bootstrap_pki`: copy the three files again (§1b) |
| Pi can't reach the laptop | Same hotspot? Firewall rule (§1a)? From the Pi: `ping ⟦LAPTOP-IP⟧` |
| Pi tile shows "measured on the Pi's console" | The Pi is running without `--power gpio`, or the INA219 gives no reading: run `python -m agent.gpio_power` on the Pi |
| LED does not blink in V2 | The Pi runs without `--power gpio`, or it was not in that run (offline): check the Pi console for "LED blinking" |
| V3 (a): no "?" tile | See V3 (a) "If not" |
| `certs\attacker\CP0045.crt.pem` not found | Run `python -m experiments.forge_cert --station-id CP0045` (§1a) |
| Server refuses to start: "--mode hybrid needs post-quantum migration" | The 1312 check fails on this laptop (quantcrypt engine): fix it with the quantcrypt steps in the dev plans |
| Everyone closed with `pq_auth_failed` after a restart | Key folder and `--db` don't match: reset both (§5) |
| W5 shows "waiting for a NEW migration" | Normal after a migration ended: it waits for the next one (V5) |
| `api` prints nothing / curl errors about the certificate | Use the Python `api` helper (§3 W6): Windows `curl.exe` cannot load PEM client certificates |
| 500 chargers: dashboard updates lag, or Task Manager shows CPU near 100 % | Fall back to the 50-charger layout (§3): stop everything, reset (§5), start §3 |
| Projector or dashboard dies | Narrate from **W5** (console migration view) and W6 `api "/api/fleet"` |

---

*Checked for v1.2 (2026-10-14, Track C, on a Linux test machine, real server, `--tls --mode hybrid`, demo profile):*

- *V1: 44 + 4 legacy + simulated CP0000 = 49 tiles; the empty panel first.*
- *V2: 45/46 hybrid, 4 legacy skipped, 1 not attempted (CP0045); server time 0.8 s.*
- *V3:*
  - *(a) forged certificate and no certificate, one `tls_rejected` line each;*
  - *(b) wrong identity: CP0045 red;*
  - *(c) late CP0045 migrated (46/46), others stayed purple;*
  - *(d) impostor cut off with 1008, repeatedly.*
- *V4: all tiles offline during the outage, back with boot checks.*
- *V5: rotation 46 rotated in 0.9 s.*
- *V7: curtailment.*

*The page was also checked against `--mock`, `--mock-halt` (15 / 10 / 25) and `--mock-n 499`.*

**Not yet run end to end:**
- *the layout on the Windows demo laptop (incl. the TLS refusal hook, see V3 a);*
- *with the real Pi (LED blink);*
- *with the halt server at the same time.*

*That is the first dress rehearsal (tracker step 17).*
