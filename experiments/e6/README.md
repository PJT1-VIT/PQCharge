# E6 — Interoperability with a third-party OCPP client

Track A (Server) · Day 10 · Runbook, configuration and results

**Question E6 answers:** does our CSMS speak real OCPP 2.0.1, or a private
dialect that only our own code understands? Every station tested before Day 10
(Track A's `fake_station.py`, Track C's agent) uses the same Python `ocpp`
library as the server, so a shared misunderstanding of the standard would never
fail. E6 connects a charger simulator **written by someone else**.

**Client:** SAP e-mobility charging stations simulator (Node.js, open source) —
https://github.com/SAP/e-mobility-charging-stations-simulator — running its own
OCPP 2.0.1 example charger (`keba-ocpp2.station-template.json`) with the changes
listed below. The simulator lives **outside** this repo; only our configuration
files, the checker and the evidence are kept here.

The other half of E6 — our agent against a third-party CSMS — is Track C's and
runs separately.

---

## Verdict — PASS (2026-09-29)

A third-party OCPP 2.0.1 charger completed **full charging sessions** on our
CSMS in **two independent runs with zero protocol errors in either direction**,
and **obeyed our remote-stop command**. The only commands it refused — power
limits — are ones this simulator does not implement in 2.0.1 mode.

| Run | Transport | Sessions | Energy (station-reported) | Messages from the charger | Errors | Verdict |
|---|---|---|---|---|---|---|
| 3 | `ws://` | 4 started / 4 ended | 87.8 Wh | Boot 1, Status 10, Authorize 1, TransactionEvent 12, Heartbeat 13 | none | **PASS** |
| 4 | `ws://` | 2 started / 2 ended (one by our `stop`) | 68.3 Wh | Boot 1, Status 5, Authorize 1, TransactionEvent 6, Heartbeat 5 | none | **PASS** |
| TLS | `wss://` | — | — | — | — | not yet run |

**CSMS → charger commands (run 4):**

| Command | Charger's answer | Meaning |
|---|---|---|
| `RequestStopTransaction` | **Accepted** | Session ended in the same second with `triggerReason: RemoteStop` — obeyed, and reported why |
| `SetChargingProfile` | `NotImplemented` CALLError | The simulator does not implement smart charging in 2.0.1 mode (its README's 2.0.x command list never included it). It echoed our payload back intact; a malformed message would have been `FormatViolation` |
| `ClearChargingProfile` | `NotImplemented` CALLError | Same limitation |

Our dispatcher handled the refusals correctly: `ok: false` with the station's
exact reason, no crash, connection kept.

### Environment (for reproduction)

| Component | Version |
|---|---|
| Simulator | commit `006c342c` |
| Node.js / pnpm | 22.14.0 / 12.6.0 |
| CSMS | branch `TrackA` at the Day 9 merge (`eb38de3`), `--migration off` |
| Python / websockets | 3.10 / **14.2**, in a dedicated venv from `requirements.txt` (see Finding 5) |
| OS | Windows, PowerShell |

Evidence: `results/e6_run3_events.jsonl`, `results/e6_run4_events.jsonl`
(Contract 3 event logs) and `results/run3_verdict.txt`, `results/run4_verdict.txt`.

---

## Findings

1. **Core protocol interoperates.** Every station-initiated message type our
   server implements was exercised by a client we did not write, and nothing was
   rejected by either side. The charger adopted the heartbeat interval from our
   `BootNotification` response (exactly 20 s apart), which proves our reply was
   read, not merely tolerated.

2. **Meter values are parsed correctly, and extra measurands are reported, not
   dropped.** The raw `TransactionEvent` carries Voltage (4 values), Current
   (4 values), Power and `Energy.Active.Import.Register`. The CSMS stored exactly
   the raw register values (checked against the raw messages: 33.94 Wh and
   3.06 Wh) and logged `voltage`, `current.import` and
   `energy.active.import.interval` by name as unrecognised — the behaviour
   `csms/metering.py` was built for.

3. **The simulator's numbers are protocol-valid but not physically consistent.**
   Examples from run 3: 3587 W for 30 s reported as 3.06 Wh (about 30 Wh
   expected); 226.7 V × 6.4 A ≈ 1.45 kW while reporting 3587 W; phase currents
   that do not match the total. Its power varies randomly between 2.0 and
   5.7 kW. **SAP energy figures must not be used in any energy result** — E6
   validates the protocol, not the physics. Energy results come from Track C's
   agent, whose power model is ours.

4. **Simulator behaviours that are not ours:** one `Authorize` for all sessions
   (it caches an accepted token, which OCPP permits); the first meter update at
   30 s regardless of our `TxUpdatedInterval` of 10 s, so each short session has
   exactly one `Updated`; charging state `EVConnected` at start.

5. **Version pinning matters.** The first attempts ran in a teammate's venv with
   `websockets 16.1.1`, outside our pin (`>=13.0,<15.0`). The recorded runs use a
   fresh venv built from `requirements.txt` (websockets 14.2).

6. **Not yet shown by E6:** a third-party charger accepting `SetChargingProfile`.
   E5's curtailment is built on it and is exercised with Track C's agent, but the
   third-party proof needs a client that implements smart charging (EVerest is the
   candidate).

---

## Files here

| File | What it is |
|---|---|
| `sap/config.json` | Simulator main config: `ws://localhost:9000`, one station from our template. From the simulator's `config-template.json`; only `supervisionUrls`, `persistState` and `stationTemplateUrls` changed |
| `sap/pqcharge-e6.station-template.json` | The simulator's own `keba-ocpp2` template with: `baseName` `E6-SAP-01` + `fixedName` (cannot clash with our `CP####` fleet), `idTagsFile` → our token, `power` 7400 W, `TxUpdatedInterval` 10 s (ignored by the simulator — Finding 4), automatic transactions **on** for 3 minutes |
| `sap/pqcharge-idtags.json` | `["TAG-0001"]` — a token our allowlist accepts, so authorisation is tested for real rather than bypassed |
| `check_e6.py` | Reads the CSMS's own logs and prints the E6 verdict |
| `results/` | Evidence from the passing runs |

---

## Runbook (Windows, PowerShell)

**Simulator quirk — read first.** `pnpm start` runs `pnpm build` first, and the
build wipes `dist\assets` and copies back only `config.json`,
`station-templates`, `json-schemas` and `configurations`. **The token file is
never copied**, so the charger's automatic transactions fail silently (the
simulator's `logs\error-<date>.log` shows `ENOENT ... pqcharge-idtags.json`).
So: build once, copy the token file into `dist\assets` yourself, and start with
`node dist/start.js` — never `pnpm start`.

```powershell
# once: CSMS venv pinned to requirements.txt
cd D:\pqcharge
py -3.10 -m venv .venv-e6
.\.venv-e6\Scripts\Activate.ps1
python -m pip install -r requirements.txt

# once: simulator, outside our repo
cd D:\
git clone https://github.com/SAP/e-mobility-charging-stations-simulator.git e6-sap-sim
cd D:\e6-sap-sim
git rev-parse --short HEAD                 # record it
npm install -g pnpm@latest                 # corepack needs Administrator on Windows
pnpm install
Copy-Item D:\pqcharge\experiments\e6\sap\config.json src\assets\config.json
Copy-Item D:\pqcharge\experiments\e6\sap\pqcharge-e6.station-template.json src\assets\station-templates\
pnpm build
Copy-Item D:\pqcharge\experiments\e6\sap\pqcharge-idtags.json dist\assets\

# window 1: CSMS (output to a file; a PowerShell 5 pipe would re-encode it)
cd D:\pqcharge
.\.venv-e6\Scripts\Activate.ps1
cmd /c "python -m csms.server --db logs\e6_runN.db --log logs\e6_runN_events.jsonl --migration off --verbose > logs\e6_runN_server.log 2>&1"

# window 2: watch it
Get-Content D:\pqcharge\logs\e6_runN_server.log -Wait

# window 3: simulator, WITHOUT the rebuild
cd D:\e6-sap-sim
$env:NODE_ENV = "production"
node dist/start.js

# window 4 (optional): commands, while a session is running
curl.exe -s "http://localhost:9000/api/stations/E6-SAP-01/stop"

# after ~4 minutes: Ctrl-C window 3, then window 1; then the verdict
python experiments\e6\check_e6.py --events logs\e6_runN_events.jsonl --server-log logs\e6_runN_server.log
```

**Pass** = the external station connected, was accepted at boot, authorised
`TAG-0001`, and started, updated and ended at least one transaction, with no
failure in a core message (Boot, Heartbeat, Status, Authorize, TransactionEvent).
An optional message we do not implement is a **finding**, not a failure.

On macOS / Git Bash the same steps apply with `/` paths, `source
.venv-e6/bin/activate`, and `| tee` in place of the `cmd /c` redirect.
