# PQCharge — Demo Runbook (A-F8, tracker step 4)

**v1.3, 2026-10-14** (v1.2 and v1.1: 13–14 Oct, v1.0: 2026-10-12). Written by Track C (Koshambi) for Track A's task A-F8, from the v0.1 draft (Track A, 2026-10-11). Lead on the day: Bhargavi.

**What changed in v1.3** (after the `demo2` rehearsal, 14 Oct):

- **Two variables, `$IP` and `$RUN`.** You set them once in every PowerShell window (§0.1). Every command uses them. A new rehearsal only changes `$RUN`.
- **The command order is the one that worked in `demo2`.** It includes the checks: `Test-Path`, the `$stamp` archive and the server-log check.
- **The analysis runs after the variants (§5), never during them.**
  - Every load generator now has `--no-analyse`.
  - After V7 you stop the fleet, run `python -m analysis.run --run $RUN`, and open the results page (V8).
  - The page opens on this run. Earlier runs, "All runs combined" and "Experiments" are one click away.
  - Running the analysis again on the same run **replaces** its result; no new file is made.
- **Each run is kept as evidence.** The analysis copies the run's `.jsonl` diaries to `evidence\demos\$RUN\` (never `.log` or `.db`).
- **The demo logs can now be analysed.** The main server and the halt server are analysed **separately**, each with its own fleet. Before, they were mixed because both use CP0001–CP0050.
- **S1 (the scale test) is gone from the results page.**
- **Lessons from `demo2`:**
  - press **Start migration once** (each press enrols every charger again, L46);
  - start each halt window **once** (CP0026–50 was started twice);
  - V3 (b), (d), (e), V6 and V7 were not run in `demo2`.

**What changed in v1.2:**
- The demo became variants V1–V8, with the attack charger CP0045 in its own window.
- New animated dashboard.
- The server logs refused TLS handshakes.
- `experiments/forge_cert.py` was added.
- The Pi's LED blinks while it is migrated.

**v1.1:**
- `--crypto-mode hybrid` on every load generator.
- The 500-charger layout.
- The Python `api` helper.
- The Pi stand-in.

> **Fill in before the first rehearsal:**
> - ⟦PI-USER@PI⟧: how we SSH into the Pi (e.g. `pi@raspberrypi.local`).
> - ⟦PI-REPO⟧: the repo folder on the Pi (e.g. `~/PQCharge`).
> - The hotspot IP goes into `$IP` (§0.1). Check it with `ipconfig` every day.
> - Demo laptop: Koshambi's Windows laptop, repo at `C:\VScodes\PQCharge`. Commands are **PowerShell**.

---

## 0. Before anything

### 0.1 Every new PowerShell window starts with these three lines

```powershell
# The repo, the laptop's hotspot IP (check with ipconfig) and THIS run's name (a new one per rehearsal: demo3, demo4, ..., final).
cd C:\VScodes\PQCharge
$IP = "172.20.10.6"
$RUN = "demo3"
```

- **Same values in every window.** A window opened without them would write to the wrong files.
- **A run name is one complete run, from the empty dashboard to the results page.** Never reuse a name: the server's `--db` and the logs belong to it.
- **Names:** letters, digits and `-` only (`demo3`, `final`, `final-2`). No `_`: files are cut at `_` (`demo3_legacy_…` belongs to `demo3`).
- **PowerShell rule:** write `${RUN}_events.jsonl`, with braces, when a `_` follows the name. `$RUN_events` would be a different, empty variable. The commands below already do this.

### 0.2 The variants (≈ 15 min) and who does what

| # | Variant | ~min | Shown on | Proves |
|---|---|---|---|---|
| V1 | **Normal charging:** 44 chargers + 4 legacy + CP0000 (Pi) connect and charge | 2 | Dashboard (tab 8080) | Classical baseline over mutual TLS; real hardware in the fleet |
| V2 | **Migration** to hybrid, from the browser: canary → waves; summary card | 2 | Dashboard + W5 + the Pi's LED | Live migration, charging never stops (core claim) |
| V3 | **Attack charger CP0045:** forged certificate, wrong identity, the real CP0045 joins and is migrated, stolen certificate without the key | 3 | Dashboard + W3 | Every kind of impostor is refused |
| V4 | Reconnect storm: every charger re-proves its key | 1.5 | Dashboard | Hybrid = a real check at every boot |
| V5 | Key rotation without disconnection | 1.5 | Dashboard + W5 | Novelty claim |
| V6 | Halt: a bad wave is rolled back, the migration stops | 1.5 | Dashboard (tab 8081) | Safety: threshold, rollback, halt |
| V7 | Curtailment: the Pi's LED dims, its mW drop | 1 | Dashboard + the LED | Cyber-physical loop |
| — | **Analysis** of this run (§5), after stopping the fleet | 1 | W6 | — |
| V8 | **Results page** of this run, then "All runs combined" | 2 | `report.html` | The numbers |

- **Driver** (types commands): Bhargavi.
- **Narrator** (talks to the panel): ⟦name⟧.
- **Pi minder** (watches the Pi console and the LED): ⟦name⟧.
- **Fleet size:** 50 (§3) or 500 (§3c), decided by tracker step 6d. V6 always uses its own 50-charger fleet.
- **Order:**
  - V3 needs V2 first (an impostor without the key is only refused once the real CP0045 has a key).
  - V5 needs a migrated fleet.
  - V6 can be shown at any point (separate server).
  - The analysis runs **only after V7**.

### 0.3 The dashboard's colours (say this once, in V1)

| Tile | Means |
|---|---|
| empty panel with a radar | no charger connected yet |
| slate | connected, not charging |
| **green**, moving light, ⚡ | charging, classical identity |
| **blue**, pulsing, ⟳ | being migrated right now |
| **purple + 🛡** (glows while charging) | post-quantum hybrid: own ML-DSA-44 key, proved |
| **burgundy**, ↩ | its wave was rolled back |
| **grey, hatched** | legacy charger (no ML-DSA-44): skipped |
| **dim, dashed**, ⏻ | offline |
| **red**, shaking, ⛔ | refused just now (attack); also a red banner and a ticker line |
| red **"?"** tile | an unknown device refused during TLS (no charger id is known then) |
| gold **C** / **W1, W2, …** / **L** | canary / its wave / legacy (before the migration) |
| 🔒 / ✓ | TLS on this connection / post-quantum key verified on this connection |

**About the speed:**
- The real server migrates a wave in a fraction of a second. The page replays the waves in order, each blue for at least 1.2 s, and says "Replaying the waves". No number is changed.
- The summary card and the results page show the server's **real** time (e.g. 0.93 s in `demo2`).

---

## 1. One-time set-up (the evening before)

### 1a. Laptop (in a window set up as §0.1)

```powershell
# Get the frozen code (tag v1.0 after CP4; main until then).
git checkout main; git pull origin main
# Must print 3.13.x (L05: every track is on 3.13).
python --version
# ML-DSA works on this laptop: must print 1312. If it fails, the hybrid server will refuse to start.
python -c "from crypto.pq import PQProvider; print(len(PQProvider().generate_keypair()[1]))"
# The whole test suite: all must pass. Run it with NO server running.
python -m pytest -q
# The laptop's hotspot IP (Wireless LAN adapter -> IPv4 Address); if it differs from $IP, fix $IP (§0.1) and redo the certificates.
ipconfig
```

**Certificates.** This makes a **new** root CA and deletes the previous run's certificate files in `certs\` (folders such as `certs\pq` are kept). Run it **once**, then copy to the Pi:
```powershell
# Certificates for CP0001-CP0500 + the Pi (CP0000); the server certificate also covers the hotspot IP.
python -m experiments.bootstrap_pki --count 500 --also CP0000 --server-san $IP
# All four must print True.
Test-Path certs\root.pem; Test-Path certs\CP0000.crt.pem; Test-Path certs\CP0000.key.pem; Test-Path certs\CP0049.crt.pem
```

**The forged certificate for V3** (after the line above; `certs\attacker\` is never committed):
```powershell
# A CP0045 certificate signed by a home-made "attacker" CA, not by our root CA.
python -m experiments.forge_cert --station-id CP0045
# Both must print True.
Test-Path certs\attacker\CP0045.crt.pem; Test-Path certs\attacker\CP0045.key.pem
```

**Firewall** (once, in an **admin** PowerShell):
```powershell
# Let the Pi reach the server on port 9000.
netsh advfirewall firewall add rule name=pqcharge dir=in action=allow protocol=TCP localport=9000
```

**Experiments view of the results page** (once the step-13 evidence is merged; optional for the demo):
```powershell
# The classical-vs-hybrid experiment runs, shown as "Experiments" on the results page.
python -m analysis.run --logs evidence\⟦final evidence folder⟧
```

**Rehearse the look without any server** (optional):
```powershell
# A pretend fleet (MOCK badge): chargers connect one by one over 8 s, then charge. The "Mock: ..." buttons play the attacks.
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
# The blink the Pi shows while it is migrated (V2): ~3 s, then the LED is off again.
python -c "import time; from agent.gpio_power import GPIOPower; p = GPIOPower(7400.0); p.indicate_migration(); time.sleep(3.5)"
```

From the laptop:
```powershell
# Copy the Pi's certificate, key and the root CA (from THIS bootstrap_pki run) to the Pi.
scp certs\CP0000.crt.pem certs\CP0000.key.pem certs\root.pem ⟦PI-USER@PI⟧:⟦PI-REPO⟧/certs/
```

Once the main server (§3) runs, on the Pi (bash: the variable is set without `$`):
```bash
# The laptop's hotspot IP, same value as $IP on the laptop.
IP=172.20.10.6
# The Pi reaches the server over mutual TLS: expect JSON with "tls": true.
curl --cacert certs/root.pem --cert certs/CP0000.crt.pem --key certs/CP0000.key.pem https://$IP:9000/api/health
```

**Where the LED logic lives** (for questions):
- `agent/gpio_power.py`:
  - `close_contactor()` → on;
  - `set_power_limit()` → dimmed;
  - `open_contactor()` → off;
  - `indicate_migration()` → blinks 3 s, then back to the above.
- `agent/station.py`: `handle_pq_enrolment()` calls it after the Pi made its new key. This happens on a migration or a key rotation only, never on a boot check.

---

## 2. Fresh state, before every rehearsal and the real demo

The chargers' key folders and the server's `--db` describe the **same** keys, so reset them **together**. Then pick a **new** `$RUN` (§0.1), which gives a new `--db` and new logs.

```powershell
# Archive the main fleet's and the halt fleet's charger keys under a time stamp (nothing is deleted).
$stamp = Get-Date -Format "MMdd_HHmm"
if (Test-Path certs\pq) { Move-Item certs\pq "certs\pq_old_$stamp" }
if (Test-Path certs\pq_halt) { Move-Item certs\pq_halt "certs\pq_halt_old_$stamp" }
# Both must print False.
Test-Path certs\pq; Test-Path certs\pq_halt
# The logs folder exists; the profiles are there (both True).
New-Item -ItemType Directory -Force logs | Out-Null
Test-Path tests\fixtures\fleet_profile_demo.json; Test-Path tests\fixtures\fleet_profile_halt_n50.json
```
```bash
# On the Pi: archive the Pi's key.
mv certs/pq certs/pq_old_$(date +%m%d_%H%M)
```
**Stop old PQCharge windows first** (Ctrl-C in each server, dashboard, watcher, load-generator and station window). Don't stop every Python process blindly.

---

## 3. Window layout and start-up (T-20 min): server and dashboard only, NO chargers yet

| Window | What runs | Started in |
|---|---|---|
| **W1** | Main server (port 9000) | §3 |
| **W6** | Commands: server-log check, `api` helper, the analysis | §3 |
| **W4** | Dashboard proxy → `localhost:8080` | §3 |
| **W5** | Console migration view (backup) | §3 |
| **W2** / **W2b** / **W7** | Fleet CP0001–44 / legacy CP0046–49 / CP0000 stand-in | V1 |
| **Pi** | CP0000 (the hardware) | V1 |
| **W3** | The attack charger CP0045 | V3 |
| **H1**, **H2a–c**, **H3** | Halt server (9001), its fleet, its proxy on `8081` | §3b |

Every window: the three lines of §0.1 first.

**W1, main server.** Ctrl-C stops it; the same command restarts it (V4):
```powershell
# Hybrid server over mutual TLS, reachable from the Pi; this run's database and logs; output to a log file.
cmd /c "python -m csms.server --host 0.0.0.0 --port 9000 --tls --mode hybrid --db logs\$RUN.db --log logs\${RUN}_events.jsonl --fleet-profile tests\fixtures\fleet_profile_demo.json --ws-ping-interval 0 > logs\${RUN}_server.log 2>&1"
```
It prints nothing: the output goes to the log.

**W6, check the server started:**
```powershell
# Expect "boot check active" and "CSMS listening" on port 9000.
Get-Content "logs\${RUN}_server.log" -Tail 15
```
If those lines are missing, `Get-Content "logs\${RUN}_server.log" -Tail 40`. **Stop if it says hybrid mode cannot start.** Never paste or share this file (L04).

**W4, dashboard proxy:**
```powershell
# Serves the dashboard on http://localhost:8080/dashboard/ and passes /api to the TLS server.
python -m dashboard.serve --api https://localhost:9000 --cert-dir certs --port 8080
```
Open tab 8080 now. It must show:
- the empty panel "No chargers connected";
- Mode hybrid, TLS 🔒, Identity check enforce, Boot key check on;
- **no MOCK badge**.

**W5, console view:**
```powershell
# Live console view of the migration; it may wait for a new migration -- that is expected. Restart it before V5.
python -m harness.watch_migration --url https://localhost:9000 --cert-dir certs
```

**W6, command helper:**
```powershell
# Defines `api`: a GET to the server with a CA-signed client certificate (Python: Windows curl.exe cannot load PEM client certificates).
function api($p) {
    $env:API_PATH = $p
    python -c "import os, urllib.request, ssl; c=ssl.create_default_context(cafile='certs/root.pem'); c.load_cert_chain(certfile='certs/CP0049.crt.pem', keyfile='certs/CP0049.key.pem'); r=urllib.request.urlopen('https://localhost:9000'+os.environ['API_PATH'], context=c, timeout=10); print('HTTP status:', r.status); print(r.read().decode())"
}
# Expect HTTP status 200, "tls": true, "identity_check": "enforce", "boot_check": true, "pq_algorithm": "ML-DSA-44".
api "/api/health"
```

### 3b. The halt server (H1–H3); start it before the panel arrives

A **second** fleet on port 9001, with Track A's refusers CP0021–25 in the middle. It has its own key folder (`certs\pq_halt`), because its chargers reuse CP0001–CP0050. **Start each H window once.** In `demo2` the H2c window was started twice, which ran CP0026–50 twice at the same time.

**H1:**
```powershell
# Second hybrid server on port 9001 with the halt profile (refusers CP0021-25 claim ML-DSA but refuse the key).
cmd /c "python -m csms.server --port 9001 --tls --mode hybrid --db logs\${RUN}_halt.db --log logs\${RUN}_halt_events.jsonl --fleet-profile tests\fixtures\fleet_profile_halt_n50.json --ws-ping-interval 0 > logs\${RUN}_halt_server.log 2>&1"
```
**H2a, H2b, H2c** (one window each):
```powershell
# Halt fleet part 1: CP0001-CP0020 (no analysis at the end: it runs after the demo, §5).
python -m harness.load_generator --n 20 --experiment "halt_$RUN" --csms-url wss://localhost:9001 --cert-dir certs --crypto-mode hybrid --pq-key-dir certs\pq_halt --charge-for 2400 --no-analyse
```
```powershell
# Track A's 5 refusers CP0021-CP0025 over TLS: they answer the enrolment request with UnknownVendorId.
python -m tests.fixtures.fake_station CP0021 CP0022 CP0023 CP0024 CP0025 --tls --cert-dir certs --url wss://localhost:9001 --charge-for 0
```
```powershell
# Halt fleet part 2: CP0026-CP0050 (same experiment name, so it is analysed as one run with part 1).
python -m harness.load_generator --n 25 --start-index 26 --experiment "halt_$RUN" --csms-url wss://localhost:9001 --cert-dir certs --crypto-mode hybrid --pq-key-dir certs\pq_halt --charge-for 2400 --no-analyse
```
**H3, its dashboard proxy:**
```powershell
# Dashboard for the halt server on http://localhost:8081/dashboard/.
python -m dashboard.serve --api https://localhost:9001 --cert-dir certs --port 8081
```
Tab 8081 shows 50 green tiles (`--charge-for 0` means the refusers charge until stopped).

### 3c. The 500-charger layout (499 simulated + the Pi) — tracker steps 6c/6d

Same windows and variants. **W1** uses the 500 profile. In V1, **W2b** starts 454 chargers instead of 4. CP0045 stays free for V3.

**W1:**
```powershell
# Hybrid server for the 500-charger demo fleet (CP0000 + CP0001-CP0449 capable, CP0450-CP0499 legacy).
cmd /c "python -m csms.server --host 0.0.0.0 --port 9000 --tls --mode hybrid --db logs\$RUN.db --log logs\${RUN}_events.jsonl --fleet-profile tests\fixtures\fleet_profile_demo_n500.json --ws-ping-interval 0 > logs\${RUN}_server.log 2>&1"
```
**W2** (V1; no `--watch-fleet` at this size, L33):
```powershell
# 44 simulated chargers CP0001-CP0044, charging for 40 min, quick reconnects.
python -m harness.load_generator --n 44 --experiment $RUN --csms-url wss://localhost:9000 --cert-dir certs --crypto-mode hybrid --pq-key-dir certs\pq --charge-for 2400 --reconnect-max-delay 5 --no-analyse
```
**W2b** (V1):
```powershell
# 454 simulated chargers CP0046-CP0499, with the laptop's CPU and loop lag recorded.
python -m harness.load_generator --n 454 --start-index 46 --experiment "${RUN}_b" --csms-url wss://localhost:9000 --cert-dir certs --crypto-mode hybrid --pq-key-dir certs\pq --charge-for 2400 --reconnect-max-delay 5 --watch-machine --no-analyse
```
Above 100 chargers the dashboard draws small squares and pre-fills **canary 10, wave 50**.

Measured on a 2-core Linux machine (13 Oct, 499 chargers, TLS, hybrid):
- all 499 connected within about a minute;
- the migration took about 5 s;
- after a server restart, all were back and checked within about 10 s;
- a rotation took about 10 s.

**Not yet measured on the Windows laptop (step 6d).** If CPU is near 100 % or the dashboard lags, use the 50 layout.

---

## 4. The variants

For each: **Do**, **See**, **Say**, **If not**.

### V1 — Normal charging: 44 + 4 legacy + CP0000 (tab 8080)

- **Do** (the panel watches the empty dashboard):
  1. **W2:**
     ```powershell
     # 44 simulated chargers CP0001-CP0044, ML-DSA capable, charging for 40 min, quick reconnects, watching the fleet.
     python -m harness.load_generator --n 44 --experiment $RUN --csms-url wss://localhost:9000 --cert-dir certs --crypto-mode hybrid --pq-key-dir certs\pq --charge-for 2400 --reconnect-max-delay 5 --watch-fleet https://localhost:9000/api/fleet --watch-every 2 --no-analyse
     ```
  2. **W2b:**
     ```powershell
     # 4 legacy chargers CP0046-CP0049 (the profile marks them classical-only, so the migration skips them).
     python -m harness.load_generator --n 4 --start-index 46 --experiment "${RUN}_legacy" --csms-url wss://localhost:9000 --cert-dir certs --crypto-mode hybrid --pq-key-dir certs\pq --charge-for 2400 --reconnect-max-delay 5 --no-analyse
     ```
  3. **CP0000.** Either **the Pi** (venv active, in ⟦PI-REPO⟧, `IP` set as in §1b):
     ```bash
     # The Pi as charger CP0000: GPIO power (LED + INA219), its own key in certs/pq, hybrid, 5 s reconnect ceiling.
     python -m agent.station --station-id CP0000 --csms-url wss://$IP:9000 --cert-dir certs --power gpio --pq-key-dir certs/pq --crypto-mode hybrid --supported-algorithms ECDSA-P256,ML-DSA-44 --charge-for 3600 --reconnect-max-delay 5
     ```
     or, **no Pi yet**, the stand-in on the laptop (**W7**):
     ```powershell
     # Simulated CP0000 (same id and certificate as the Pi, simulated power).
     python -m agent.station --station-id CP0000 --csms-url wss://localhost:9000 --cert-dir certs --pq-key-dir certs\pq --crypto-mode hybrid --charge-for 2400 --reconnect-max-delay 5
     ```
     Before the real Pi joins a fleet the stand-in was in, reset (§2): the stand-in enrolled its own key.
  4. Click the Pi tile (CP0000), any other tile, then a legacy tile (CP0046).
- **See:**
  - **Tiles pop in** as chargers connect, then turn **green** about a second later. The stepper goes Connect → Charge.
  - The header shows **49 connected, 49 charging** (CP0045 is V3's).
  - CP0046–49 carry an **L**.
  - The Pi tile shows "LED 100 % · 7.4 kW (scaled)" and its INA219 mW, and **the LED is on**.
  - The inspector shows TLS 1.3, "identity ✓", and no post-quantum key yet.
- **Say:** "Forty-nine chargers, one of them real hardware, all on mutual TLS. Four are older models that can't do post-quantum. All classical today, all charging."
- **If not:**
  - If you see `pq_auth_failed`, repeated 1008, or key errors in W2: **stop**. The key folder and `--db` don't match: reset (§2) with a new `$RUN`.
  - The Pi is missing: check `$IP`, the firewall, and the `curl` of §1b.
  - Tiles stay slate: wait 2–3 s.

### V2 — Migration, from the browser (tab 8080 + W5 + the LED)

- **Do:**
  1. In **Controls**: **Canary 5**, **Wave size 10** (500 layout: 10 and 50, pre-filled).
  2. Say the "Say" line, then press **Start migration → hybrid — once.** Each press starts a new run that enrols every charger again (L46). In `demo2` it was pressed twice, 5 s apart.
- **See:**
  - Charge → Canary → Waves → Hybrid.
  - Canary tiles (gold **C**; the Pi is one) go **blue**, then **purple + 🛡**; then W1, W2, …
  - **The Pi's LED blinks ~3 s.**
  - CP0046–49 turn **grey, hatched**. The power line does not dip.
  - **Summary card:** "✅ Migration complete", **45/46 hybrid**, 4 legacy skipped, **1 not attempted** (CP0045, not connected yet, "deferred"), the server time (well under a second), key-check median, "Charging 49 → 49".
- **Say:** "Each charger makes its own key on the spot. Only the public half travels. It must prove it holds the private half, and nobody stopped charging. The server did this in under a second; the screen slows it down so we can follow."
- **If not:**
  - A 409 means a run is already going.
  - Fallback in W6: `api "/api/migration/start?wave_size=10&canary_count=5&target_mode=hybrid"`.

### V3 — The attack charger CP0045, while the others charge (W3 + tab 8080)

The other 49 keep charging throughout. Point at **Refused** in the header.

**(a) Forged (or old) certificate: refused during TLS.**
```powershell
# CP0045 with a certificate our CA never signed (made in §1a); it trusts our server, our server must not trust it.
python -m agent.station --station-id CP0045 --csms-url wss://localhost:9000 --cert certs\attacker\CP0045.crt.pem --key certs\attacker\CP0045.key.pem --ca certs\root.pem --pq-key-dir none --crypto-mode hybrid --reconnect-max-attempts 1
```
```powershell
# Optional: the same with NO client certificate at all.
python -m experiments.forge_cert --probe-no-cert --host localhost --port 9000
```
- **See:**
  - A red **"?" unknown device** tile.
  - The banner: "refused at the TLS handshake — certificate not signed by our CA". For the probe: "no client certificate presented".
  - Each run of the command adds one (×1, ×2, …).
- **Say:** "It claims to be charger 45, but someone else's authority signed its certificate. It is stopped before it can say a word — so the server doesn't even know which charger it pretended to be. An expired certificate from an old CA is refused the same way."
- **If not** (no "?" tile; L44, checked on Linux only): run `Select-String -Path "logs\${RUN}_events.jsonl" -Pattern tls_rejected`. If there is nothing, tell Track C. The refusal itself still works.

**(b) Wrong identity: a real certificate, but another charger's.**
```powershell
# CP0004's genuine certificate used as CP0045: refused by the identity check (1008) before it ever boots.
python -m agent.station --station-id CP0045 --csms-url wss://localhost:9000 --cert certs\CP0004.crt.pem --key certs\CP0004.key.pem --ca certs\root.pem --pq-key-dir none --crypto-mode hybrid --reconnect-max-attempts 1
```
- **See:**
  - The **CP0045 tile appears red** and shakes.
  - The banner: "wrong identity: it presented the certificate of CP0004".
  - After ~8 s the tile turns dim, with a **⛔1** flag.
- **Say:** "A real certificate from our own authority, stolen from charger 4, used as charger 45. The name doesn't match, so it's cut off."

**(c) The real CP0045 joins late, and is migrated.**
```powershell
# The real CP0045 with its own certificate, joining a running fleet.
python -m agent.station --station-id CP0045 --csms-url wss://localhost:9000 --cert-dir certs --pq-key-dir certs\pq --crypto-mode hybrid --charge-for 2400 --reconnect-max-delay 5
```
When its tile is green, press **Start migration → hybrid** again.
- **See:**
  - CP0045 goes blue, then purple; the others stay purple.
  - Summary: **46/46**, 0 not attempted.
  - **The Pi's LED blinks again**, and about 46 key checks run: a new run enrols every capable charger again (L46). It is not a fault.
- **Say:** "A charger offline during the migration was deferred, not failed. It joins later and is upgraded on the next run."

**(d) Stolen certificate without the key: refused at every boot (1008).** **Ctrl-C** the real CP0045 in W3, then:
```powershell
# Impostor: CP0045's genuine TLS certificate, but no ML-DSA key; retries every 1-2 s.
python -m agent.station --station-id CP0045 --csms-url wss://localhost:9000 --cert-dir certs --pq-key-dir none --crypto-mode hybrid --reconnect-base-delay 1 --reconnect-max-delay 2
```
- **See:**
  - CP0045 goes **red** again and again; "failed its post-quantum key check → cut off (1008)".
  - **Refused** keeps counting.
  - W3 shows `code=1008 reason='pq_auth_failed'`.
- **Say:** "Now the certificate is valid and the name matches: it was stolen from charger 45 itself. Before the migration this would have worked. Now it can't answer the post-quantum challenge, so it's cut off, every time."

**(e) Back to normal:** **Ctrl-C** the impostor, then run the (c) command again. Within ~8 s CP0045 is **purple** again.

### V4 — Reconnect storm (main)

- **Do:** W1 **Ctrl-C**, count to 10 out loud, run the **same W1 command** again (same window, same `$RUN`).
- **See:**
  - "server not reachable"; every tile goes **dim and dashed**.
  - Within ~10 s they come back **purple** (legacy grey), with "✓ … (boot)" for every migrated charger. "Recovered" climbs back to 50.
  - The migration panel says IDLE (new server run): normal.
- **Say:** "After the restart, every migrated charger had to prove its key again before it counted as recovered."
- **If not:** chargers stay dim for more than 30 s → back-off, wait. Many `pq_auth_failed` → key folder and `--db` don't match (§2).

### V5 — Key rotation (tab 8080)

- **Do:**
  1. **Restart W5** first (the §3 command); it exited after V2.
  2. Press **Rotate keys** (canary 5, wave 10).
- **See:**
  - ROTATION; tiles stay purple with a **violet ring** wave by wave.
  - "↻ CP00xx key rotated · `aaaa…` → `bbbb…` · no disconnect".
  - The Pi's LED blinks.
  - Summary: 46 rotated, 0 kept old key. **No tile goes dim**; the power line is flat.
- **Say:** "Every charger gets a new key while the old one stays valid until the new one is proven. Nobody disconnects."
- **If not:** a 409 means a migration is still finishing. Fallback: `api "/api/migration/rotate?wave_size=10&canary_count=5"`.

### V6 — Halt (switch to tab 8081)

- **Do:** Start migration (canary 5, wave 10) **on tab 8081**.
- **See:**
  - The canary and W1 go purple.
  - W2 (CP0016–25) goes blue, then the **whole wave turns burgundy ↩**: "Wave 2 rolled back — more than 20 % failed; the migration halted".
  - W3–W5 stay green, "not attempted".
  - Summary: **15/50 hybrid, 10 rolled back, 25 not attempted**.
- **Say:** "Too many failures in one wave: we put that wave back and stop. Nothing beyond it is touched."
- **Then:** back to tab 8080.

### V7 — Curtailment (tab 8080 + the LED)

- **Do:** slider to **3.7 kW**, **Apply limit**; then **Clear limit**.
- **See:**
  - Fleet power drops to about half, then recovers.
  - **The LED dims**, then brightens; the Pi tile's "LED 50 %" and mW drop with it.
- **Say:** "One command from the operator changes physical power across the fleet. That's exactly why the identity checks matter."
- **If not:** `api "/api/fleet/limit?watts=3700"` then `api "/api/fleet/clear-limit"`.

---

## 5. Analysis of this run — after V7, never before

1. **Stop the fleet,** so every diary is complete. Ctrl-C in W2, W2b, W3, W7, H2a–c, the Pi, then W1 and H1. The dashboards can stay open. With `--no-analyse`, nothing analyses by itself.
2. **W6:**
   ```powershell
   # Analyse THIS run only: its files are copied to evidence\demos\$RUN, the result replaces any earlier analysis of $RUN.
   python -m analysis.run --run $RUN
   ```
   It prints one line per variant. For example (the `demo2` rehearsal):
   ```
   demo2: trust=warn  chargers=50  connect median=2038.7 ms
     V2 migration: 46/46 hybrid, 4 legacy, 0 rolled back, 0 not attempted; server time 0.93 s; key check median 54.8 ms; 3 migration run(s); ...
     V3 attacks: TLS refused 1, wrong identity 0, stolen certificate cut off 0 (boot checks failed 0)
     V4 storm: 95% recovered in 4.61 s (48/48)
     V5 rotation: 46 rotated, 0 failed
     V6 halt: halt fleet ran, but no migration was started on it
     V7 curtailment: not_seen
   ```
   `trust=warn` is normal here: the testers were stopped with Ctrl-C, so the run is marked "incomplete".
3. **Running it again on the same `$RUN` replaces** `analysis\output\runs\$RUN.json` and `evidence\demos\$RUN\`. No new file is ever added.
4. Another run can be analysed or re-analysed at any time:
   - `python -m analysis.run --run demo2`;
   - `--all-runs` re-analyses everything in `evidence\demos\` (e.g. after `git pull` on another laptop).

## V8 — The results page

- **Do:**
  ```powershell
  # Open the results page (it opens on the run analysed last).
  start analysis\output\report.html
  ```
- **See:**
  - The bar at the top: **`$RUN` (analysed last)**, the earlier demo runs, **All runs combined**, and **Experiments (step 13)** if analysed (§1a).
  - **`$RUN`:** a V1–V7 table with ✓ per variant, the tiles (chargers, post-quantum, key-check median, storm, attacks refused), then Runs, E1–E5 and the Pi/external chargers. Main and halt servers appear as separate rows.
  - **All runs combined:**
    - every run side by side (hybrid, server time, key-check median, storm, rotation, halt, attacks);
    - the pooled key-check times (per run, and every check together);
    - the pooled connect times.
- **Say:** the V-table line by line; then "and over all our rehearsals" on the combined view.

---

## 6. Reset between rehearsals (5 min)

1. Everything stopped (§5, step 1), then the dashboards (W4, W5, H3).
2. §2: archive the keys (`$stamp`), and set a **new** `$RUN` in every window (§0.1).
3. **Reload the browser tabs.**
4. Start again at §3. **Don't** re-run `bootstrap_pki` (the Pi's certificates would no longer match). `certs\attacker\` can stay.

---

## 7. After the demo

- **Keep the run:**
  ```powershell
  # Commit THIS run's evidence (only .jsonl; about 25-30 MB for a 50-charger run). Never git add . on the demo laptop.
  git add "evidence/demos/$RUN"
  git commit -m "Demo run $RUN: evidence (main + halt diaries)"
  git push origin TrackC
  ```
- **Never commit or share** `logs\`, `*.db`, `*_server.log`, `certs\` (including `certs\attacker\`) or `analysis\output*\`. `.gitignore` covers them; check with `git status` before every commit.
- **What the demo runs are, and are not:** a demo run is analysed main and halt apart, so its numbers are sound for that run. The classical-vs-hybrid comparison (E1/E2) still comes from the step-13 experiment runs ("Experiments" view): a demo run is hybrid only.
- Attack numbers from the command line, if asked:
  ```powershell
  # The three E5 verdicts (stolen certificate / wrong identity / curtailment) for this run.
  python -m experiments.e5_check "logs\${RUN}_events.jsonl"
  ```
  The forged-certificate refusals of V3 (a) are not one of these verdicts (L48). They are on the results page (V3 row and E5 "Refused at TLS").

---

## 8. Quick fixes

| Symptom | Fix |
|---|---|
| A command writes `logs\.db` or `logs\_events.jsonl` | `$RUN` is not set in that window: §0.1 |
| `logs\demo3_events` missing, but `logs\demo3` exists | `$RUN_events` was typed without braces: use `${RUN}_events` |
| Dashboard blank or shows **MOCK** | Wrong tab, or W4 not running: open `http://localhost:8080/dashboard/` |
| Dashboard shows tiles before V1 | Old chargers still running, or the page not reloaded |
| Port 9000 / 9001 already in use | An old server is still running: close its window |
| `pq_auth_failed` / repeated 1008 at V1 | Key folder and `--db` don't match: §2 with a new `$RUN` |
| Pi: `certificate verify failed` | The Pi has older certificates: copy again (§1b) |
| Pi can't reach the laptop | Same hotspot? `$IP` right? Firewall (§1a)? `ping $IP` from the Pi |
| LED does not blink in V2 | The Pi runs without `--power gpio`, or it was offline during the wave |
| V3 (a): no "?" tile | V3 (a) "If not" (L44) |
| `certs\attacker\CP0045.crt.pem` not found | `python -m experiments.forge_cert --station-id CP0045` |
| `analysis.run --run $RUN`: "no file of run" | Wrong name, or the logs are elsewhere: `--logs <folder>` |
| Results page shows an old run first | It opens on the run analysed **last**: analyse `$RUN` again, or click it |
| W5 says "waiting for a NEW migration" | Normal after a migration ended |
| `api` errors about the certificate | Use the Python `api` helper (§3 W6) |
| 500 chargers lag | 50 layout: stop, §2, start §3 |
| Projector or dashboard dies | Narrate from W5 and `api "/api/fleet"` |

---

*Checked for v1.3 (2026-10-14, Track C):*
- *The analysis commands were run on the real `demo2` logs: the output in §5 is from them. The halt part was analysed apart (CP0026–50 started twice → one run of 45 plus one of 25, not 70).*
- *Re-analysing a run replaces its files; a run only in `evidence\demos\` is analysed from there.*
- *The `$RUN` / `${RUN}_…` spelling follows PowerShell's variable rules. **Not yet typed on the Windows laptop: check it at the next rehearsal.***
- *Not yet run end to end: V3 (b), (d), (e), V6 and V7 on the real server; the Pi.*
