# PQCharge live dashboard (Track C, C-F6)

The live fleet view for the demo: every charger as a tile, the migration as it
happens, a ticker of key checks, a security inspector per charger, fleet power
and key-check times, and the controls (migrate, rotate, roll back, power limit).

Three plain files in `static/` (no build step, no new package); ECharts comes
from `static/vendor/` (a copy of `analysis/web/vendor/`, Apache 2.0).

## Run it

| Situation | Command | Open |
|---|---|---|
| Rehearse with no server (made-up data, page says **MOCK**) | `python -m dashboard.serve --mock` | http://localhost:8080/dashboard/ |
| Same, with the halt (Track B's L09 fleet: CP0001–50, no Pi; CP0021–25 refuse their key) | `python -m dashboard.serve --mock --mock-halt` | same |
| Real CSMS, plain `ws://` (the CSMS serves the page itself, A-F4) | nothing extra | http://localhost:9000/dashboard/ |
| Real CSMS over TLS (a browser has no client certificate, so go through the proxy) | `python -m dashboard.serve --api https://localhost:9000 --cert-dir certs` | http://localhost:8080/dashboard/ |

On a projector: full screen (F11) and browser zoom to taste; the layout stacks
below 1100 px wide.

**Large fleets (tracker step 6c):** above 100 chargers each charger is drawn as
a small colour square, so the whole fleet fits on one screen (a white dot =
post-quantum key verified on this connection; purple = new key proven in the
current key rotation). The Pi keeps its large tile, and Controls are pre-filled
with canary 10 / wave size 50. Rehearse it with `python -m dashboard.serve
--mock --mock-n 499` (the mock then follows `fleet_profile_demo_n500.json`:
CP0450–CP0499 legacy).

## What it reads (GET only)

- `/api/fleet` every 1 s: every charger + the migration status (Contract 6/4).
- `/api/events?after=<seq>` every 0.5 s: the ticker (A-F4). **Without it** the
  ticker is built from what changed between two fleet polls, and the page says so.
- `/api/health` every 5 s: mode, TLS, identity check, algorithm, boot key
  check on/off (A-F4 fields).

Fields the inspector uses from A-F4: `tls_cipher`, `identity_ok`, `pq_key_id`,
`last_pq_check`. The Pi is **CP0000** (its tile spans three columns); it uses
`measured_mw` (the INA219 reading, L37: sent by the Pi as OCPP customData,
B-F10, and shown on the station row by the server, A-F10). If the Pi runs
without its sensor, the tile says "measured: on the Pi's console".

The mock is the demo fleet (`tests/fixtures/fleet_profile_demo.json`): CP0000
the Pi, CP0001–CP0045 capable, CP0046–CP0049 legacy. Its impostor button
attacks **CP0045**, the demo victim of `experiments/e5_attack.md`.

"Recovered" is Track A's `is_recovered` rule restated for display; every other
number is the server's own.
