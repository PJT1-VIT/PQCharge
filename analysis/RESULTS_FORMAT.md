# analysis/output/results.json — format v3

The contract between **C6 (analysis)**, which writes this file, and **C7 (dashboard)**, which
reads it. Written by `python -m analysis.run` and automatically at the end of every
load-generator run. Rebuilt from the diaries every time, so deleting it loses nothing.

`results.js` holds the same object as `window.PQCHARGE_RESULTS = {...};` so that
`report.html` works when opened straight from disk.

## Top level

| Key | What it is |
|---|---|
| `format_version` | `3` (C-P4). Bumped when a field's meaning changes or a field is removed. v2: E3 counts the **whole fleet**; new E3/E5/node fields; several server diaries. **v3 (Contract 7):** E3's key checks are the migration's only (boot checks apart); E2's "recovered" depends on the mode; new E1 ready times. |
| `generated_at` | When the analysis ran (ISO-8601 UTC). The page reloads when this changes. |
| `sources` | Which diaries were read (`server_diaries[]`), how many lines, duplicates dropped, lines whose charger id came from `payload.station`, runs found / kept. |
| `diary_check` | `{status, issues[]}` for the server diary as a whole. |
| `slot_order` | Slot keys in display order. |
| `slots` | `{slot_key: slot}` — one per test setup, newest run wins. |
| `comparisons` | Cross-run series: `e1_vs_n`, `e2_vs_n`, `overhead_vs_classical` (connection time); **v3:** `ready_vs_n`, `ready_overhead_vs_classical` (`ready_ms`, with `basis` and `added_median_ms`). **C-P6 (S1, additive):** `machine_vs_n` — per series, one row per run: `n`, `cpu_p95`, `loop_lag_p95`, `tester_cpu_p95`, `tester_rss_mb_max`, `server_cpu_p95`, `server_rss_mb_max`, `saturated`. |
| `e4` | `{limits, rows[], notes[]}` — artifact sizes (not tied to a run). **C-F5:** the wire rows are Contract 7's (`RequestPQEnrolment` request and reply, challenge, signed answer; the `InstallPQAuth` row is gone); ML-DSA certificate rows are added from Track B's `crypto/pq_x509.size_rows()` when available (cryptography 50+), else a note says why. |
| `external_nodes` | Chargers the tester did not start: the Pi (`kind: "hardware"`, CP0100) and E6 clients. Each has `energy_source: "charger-reported"` (never add it to a fleet total), `pq_auth` (last key-check result) and `deferred`. |

## A slot

Key: `experiment|n<N>|<mode>|tls` or `...|plain` — e.g. `e2|n500|pqc|tls`.

| Key | What it is |
|---|---|
| `experiment`, `n_stations`, `crypto_mode`, `tls` | The setup. |
| `harness_run_id`, `server_run_ids[]` | The tester's run id; every server run id seen (several after an E2 restart). |
| `started_at`, `finished_at`, `wall_s`, `completed` | Timing; `completed=false` means the run was stopped early. |
| `trust` | `{status: pass/warn/fail, issues[{level, code, message}]}`. **Show it next to every number.** |
| `overview` | `stations`, `connections`, `sessions`, `meter`, `offline_queue`, and `timeline` (`connected`, `charging`, `power_w` as `[[seconds, value], ...]`). |
| `e1` | `station_connect_ms` (TCP + TLS + WebSocket), `reconnect_connect_ms`, `server_upgrade_ms`, `bytes_per_connection` — each a *distribution* (below) — plus `tls`. **v3:** `boot_ready_ms` (dial → boot accepted), `secure_ready_ms` (dial → first key check answered, connections opened with a key held), `secure_ready_reconnect_ms`, `ready_ms` + `ready_basis` (the run's own "ready": secure-ready in a hybrid run that has it, else boot accepted), `sign_ms`, `chargers_with_key_at_connect`. First connections only, except `*_reconnect_*`. |
| `e2` | `null` unless the server restarted in the run. Else `t50_s`, `t95_s`, `t100_s` (`null` = never reached), `population`, `recovered`, `unrecovered`, `curve`, `snapshot_curve`, `integrity{...}`. **v3:** `recovery_rule` (`boot` / `boot + key check`), `recovered_definition`, `key_checked_population`, `boot_checks_failed_after_restart`, `booted_but_key_check_not_passed[]` (Contract 7 §7.7). |
| `e3` | `null` unless a migration happened. Else, for the **whole fleet**: `final_counts`, `state_timeline{state: [[s, count]]}`, `fleet_size`, `tester_chargers`; the controller's own `controller_counts` and `controller_sum_violations`; `waves[]`, `markers[]`, `rollbacks`, `manual_rollbacks`, `failures[]`, `duration_s`, `phase`; `verification` (`authenticated` / `key installed (not authenticated)` / `partly authenticated` / `nothing migrated`); `pq_checks{total, passed, rejected, rejections[], algorithm, round_trip_ms, first_per_charger_ms, later_ms}`; `deferred{count, station_ids}`; `agent_view{available, migrated_but_no_key[]}`; `charging{...}` (tester's chargers). **v3:** `pq_checks` counts `trigger: "migration"` only (no trigger = migration); `boot_checks{total, passed, rejected, round_trip_ms}`; `pq_enrolled{count, key_ids{}}`; `keys_at_start{known, count, station_ids[]}`. **C-F5 (additive):** `kind` (`migration`, or `rotation` for a run that only rotated keys); `halt{rolled_back_wave, never_attempted, shown}` (L09: shown = rolled back while chargers were still waiting); `rotation` (`null`, or `{completed, failed, checks{total, passed, rejected, round_trip_ms}, keys[{station_id, ok, old_key_id, new_key_id, wave_id, detail, at_s}]}` from Track B's `rotation_completed`/`rotation_failed` lines and `pq_auth` lines with `trigger: "rotation"`, which are never counted in `pq_checks`). |
| `e5` | `identity_checks`, `identity_rejected`, `identity_mismatches` (let in by `warn` mode), `identity_mode`, `identity_rejections[]`, `pq_checks`, `pq_passed`, `pq_rejected`, `pq_rejections[]` (v3: each with `trigger`), `connection_failures{}`, `callerrors`, `commands_received`. **v3:** `pq_by_trigger{migration, boot}` (each `{checks, passed, rejected}`), `pq_cut_off`, `pq_cut_off_ids[]` (closed with reason `pq_auth_failed`). |
| `machine` | **C-P6 (S1, additive; `null` without `--watch-machine`).** `samples`, `cpu_count`, `cpu_pct`, `mem_pct`, `loop_lag_ms` (each `{n, median, p95, max}`, not a full distribution), `mem_used_mb_max`, `tester{cpu_pct, rss_mb_max, threads_max}`, `server{found, samples, cpu_pct, rss_mb_max, threads_max}`, `timeline{cpu_pct, loop_lag_ms, tester_cpu_pct, server_cpu_pct, tester_rss_mb}` as `[[s, value]]`, `limits{cpu_p95_pct: 85, loop_lag_p95_ms: 50}`, `saturated`, `saturation_reasons[]`. Tester/server CPU is % of ONE core. |

## A distribution

`{n, mean, stdev, min, median, median_ci95: [low, high], p90, p95, p99, max,
box: {whisker_low, q1, median, q3, whisker_high, outliers[]}, ecdf: [[value, percent], ...]}`
— or just `{n: 0}` when there is no data. Times are milliseconds unless the key ends in `_s`.

## Rules for readers

- Never recompute a number the file already has; format it. (Same rule as Track A's
  `aggregate_power_w`: one place computes, everyone else displays.)
- `null` means "not measured / not reached". Never show it as `0`.
- Modes are `classical`, `hybrid`, `pqc`. Colours: blue, orange, aqua — the same on every page.

## How the server diary is read (C6.1)

- **Every** server diary is read: `--events` (repeatable) or, by default, every
  `*events*.jsonl` in `--logs` whose content is a server diary. A line found in
  two files counts once.
- Files are recognised by their **content**, not only their name.
- Track B's orchestrator lines have an empty top-level `station_id`/`outcome`;
  the analysis fills them from `payload.station` / `payload.result` in memory
  only (the file is never changed). A filled top-level field is never replaced.

## Contract 7 lines read (v3)

- Tester diary (Track C): `station_connected.pq_key_held` / `pq_key_id`; `station_booted`
  `{connection, since_connect_ms}`; `station_authenticated` `{connection, challenge_no,
  since_connect_ms, sign_ms, key_id}`; `station_finished.pq_key_held_at_start`.
- Server diary: `pq_auth` lines' `payload.trigger` (`migration` / `boot`);
  `certificate_installed` with `transition: "pq_enrolled"`; `connection_closed` with
  `reason: "pq_auth_failed"`; every line's `crypto_mode` (compared with the tester's mode).
- New trust checks: `mode_mismatch` (warn), `started_with_saved_keys` (warn in a migration
  run, else info), `hybrid_without_boot_checks` (warn).
- C-P6: `machine_saturated` (warn) when machine CPU p95 > 85 % or tester loop lag p95 > 50 ms.
  `machine` and `machine_vs_n` are additive, so the format stays v3.
