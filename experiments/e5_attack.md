# E5 — attacks the server must stop (Track A, A-F7)

Three attacks, run against a **hybrid** server over **mutual TLS**. Each one is
stopped (or, for curtailment, carried out) by the server, and each leaves lines
in the server's event log that Track C's E5 analysis and
`python -m experiments.e5_check` count.

| # | Attack | What the attacker has | What the server does | Proves |
|---|---|---|---|---|
| a | **Stolen certificate** | A migrated charger's genuine TLS certificate and key, but **not** its ML-DSA private key | TLS and the identity check pass. The boot key check is **Rejected** → connection **closed 1008** (`pq_auth_failed`), again on every reconnect | After migration, a classical identity alone no longer gets a charger in (Contract 7 §7.5) |
| b | **Wrong identity** | Another charger's genuine certificate (CP0004's), used on CP0003's id | Refused by the identity check (`enforce`, the default since L07): closed 1008 `certificate identity mismatch` | Mutual TLS + CN-to-id binding (finding F1) |
| c | **Curtailment** | Access to the operator API | Every charger's power limit is set, then cleared; fleet power drops and recovers | The cyber-physical loop: one command changes physical power across the fleet |

Checked live on 2026-10-10 (Linux, Python 3.13, 3 chargers, mutual TLS 1.3):
(a) refused 20 times in 15 s, every attempt closed 1008; (b) refused once with
`certificate identity mismatch`; (c) 14.8 kW → 7.4 kW → 14.8 kW. The real
CP0003 was let back in with its own key afterwards (boot check passed, 12 ms).

---

## Rules before you start

- **Fresh pair:** a new `--db` name **and** an empty charger key folder
  (`certs/pq`) together (see "charger keys persist" in the dev plans). Otherwise
  old keys make the counts confusing.
- **Certificates:** `python -m experiments.bootstrap_pki --count 50` once
  (the victim and the "wrong identity" certificate must both exist).
- **The victim runs in its own window** (not inside the load generator), so it
  can be stopped on its own. Below the victim is **CP0003** for the small run,
  and **CP0045** for the demo fleet.
- Commands are **PowerShell** (Windows). On macOS/Linux use `/` instead of `\`
  and `curl` instead of `curl.exe`.

`/api` needs a client certificate under TLS. In every window that calls it:

```powershell
function api($p) { curl.exe -s --cacert certs\root.pem --cert certs\CP0004.crt.pem --key certs\CP0004.key.pem "https://localhost:9000$p" }
```
On macOS/Linux:
```bash
api() { curl -s --cacert certs/root.pem --cert certs/CP0004.crt.pem --key certs/CP0004.key.pem "https://localhost:9000$1"; }
```

(Any CA-signed certificate opens `/api`; L08. The dashboard proxy does the
same.)

---

## Small run (N = 3 + victim) — for E5 evidence

**W1 — server (hybrid, TLS, fresh pair)**
```powershell
move certs\pq certs\pq_archive_e5 2>$null
cmd /c "python -m csms.server --mode hybrid --tls --db logs\e5.db --log logs\e5_events.jsonl --fleet-profile tests\fixtures\fleet_profile_day9_clean.json --ws-ping-interval 0 > logs\e5_server.log 2>&1"
```
Wait for `boot check active` in `logs\e5_server.log`.

**W2 — fleet (CP0001, CP0002)**
```powershell
python -m harness.load_generator --n 2 --experiment e5 --csms-url wss://localhost:9000 --cert-dir certs --pq-key-dir certs\pq --charge-for 600
```

**W3 — the victim (CP0003, its own window)**
```powershell
python -m agent.station --station-id CP0003 --csms-url wss://localhost:9000 --cert-dir certs --pq-key-dir certs\pq --crypto-mode hybrid --charge-for 600
```

**W4 — migrate** (when 3 are connected)
```powershell
api "/api/migration/start?wave_size=2&canary_count=1&target_mode=hybrid"
api "/api/fleet"      # CP0001-0003 "migrated", each with a pq_key_id
```

### (a) Stolen certificate

1. **W3: Ctrl-C** (the real CP0003 goes offline).
2. **W3: the impostor** — same id, same certificate, **no key folder**:
   ```powershell
   python -m agent.station --station-id CP0003 --csms-url wss://localhost:9000 --cert-dir certs --pq-key-dir none --crypto-mode hybrid --charge-for 600 --reconnect-base-delay 1 --reconnect-max-delay 2
   ```
   **Expected:** it connects, boots, then is closed: agent log
   `connection closed: code=1008 reason='pq_auth_failed'`, then it reconnects
   and is closed again, every time. Server log:
   `boot key check FAILED for CP0003: closing the connection (1008, pq_auth_failed)`.
   Dashboard: red flashes on CP0003 in the ticker.
3. **Ctrl-C** after ~15 s.

### (b) Wrong identity

**W3:**
```powershell
python -m agent.station --station-id CP0003 --csms-url wss://localhost:9000 --cert certs\CP0004.crt.pem --key certs\CP0004.key.pem --ca certs\root.pem --pq-key-dir none --crypto-mode hybrid --reconnect-max-attempts 1
```
**Expected:** `connection closed: code=1008 reason='certificate identity mismatch'`,
then `giving up after 1 attempt(s)`. It never reaches the boot check.

### (c) Curtailment

**W4:**
```powershell
api "/api/fleet/limit?watts=3700"     # targets = connected chargers, ok = same number
api "/api/fleet"                      # aggregate_power_w drops (3.7 kW each)
api "/api/fleet/clear-limit"
api "/api/fleet"                      # back to 7.4 kW each
```

### Put the real victim back (optional, good for the panel)

**W3:** the first W3 command again (with `--pq-key-dir certs\pq`). Expected:
`api "/api/fleet/CP0003"` → `pq_verified: true`, `last_pq_check.trigger: "boot"`,
`result: "success"`.

### Verdict and evidence

```powershell
python -m experiments.e5_check logs\e5_events.jsonl
```
Expected:
```
(a) stolen certificate, no ML-DSA key
    boot key checks: N rejected, ...
    connections closed 1008 (pq_auth_failed): N {'CP0003': N}
    -> STOPPED
(b) wrong identity (another station's certificate)
    refused: connected as CP0003 with a certificate for CP0004
    -> STOPPED
(c) curtailment (cyber-physical actuation)
    ClearChargingProfile: 2 x success
    SetChargingProfile: 2 x success
    -> DONE
```
Then stop W2 and W1 (Ctrl-C) and give **`logs\e5_events.jsonl`** (and the load
generator's `*.jsonl` diary) to Track C for C-F4. **Never** share
`e5_server.log` or anything in `certs\`.

---

## During the demo (scene 5 and 6, demo fleet)

Fleet profile `tests\fixtures\fleet_profile_demo.json`: CP0000 = the Pi,
CP0001–CP0045 capable, CP0046–CP0049 legacy. The victim is **CP0045**:

```powershell
# W2a: CP0001-CP0044
python -m harness.load_generator --n 44 --experiment demo --csms-url wss://<laptop>:9000 --cert-dir certs --pq-key-dir certs\pq --charge-for 1800
# W2b: the legacy chargers CP0046-CP0049
python -m harness.load_generator --n 4 --start-index 46 --experiment demo_legacy --csms-url wss://<laptop>:9000 --cert-dir certs --pq-key-dir certs\pq --charge-for 1800
# W3: the victim CP0045, alone
python -m agent.station --station-id CP0045 --csms-url wss://<laptop>:9000 --cert-dir certs --pq-key-dir certs\pq --crypto-mode hybrid --charge-for 1800
```
After the migration (scene 2), run (a) with `CP0045` in place of `CP0003`.
Curtailment (scene 6) is the dashboard's power-limit slider; it calls the
same `/api/fleet/limit` and `/api/fleet/clear-limit`.

## If something looks wrong

| Symptom | Cause | Fix |
|---|---|---|
| Impostor is **not** closed, stays connected | Server not in `--mode hybrid`, or CP0003 was never migrated | `api "/api/health"` must show `"boot_check": true`; `api "/api/fleet/CP0003"` must show `migration_state: "migrated"` |
| The **real** CP0003 is closed with `pq_auth_failed` | Its key folder and the server `--db` do not match (one was reset) | Reset **both** (new `--db`, empty `certs\pq`) and migrate again |
| (b) connects instead of being refused | Server started with `--tls-identity-check warn` or without `--tls` | Restart with `--tls` and no `--tls-identity-check` (default is `enforce`) |
| `curl` fails with a certificate error | Not in the repo folder, or certificates regenerated after the server started | `cd` to the repo; restart the server after `bootstrap_pki` |
