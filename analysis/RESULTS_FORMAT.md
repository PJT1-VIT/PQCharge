# analysis/output/results.json — format v1

The contract between **C6 (analysis)**, which writes this file, and **C7 (dashboard)**, which
reads it. Written by `python -m analysis.run` and automatically at the end of every
load-generator run. Rebuilt from the diaries every time, so deleting it loses nothing.

`results.js` holds the same object as `window.PQCHARGE_RESULTS = {...};` so that
`report.html` works when opened straight from disk.

## Top level

| Key | What it is |
|---|---|
| `format_version` | `1`. Bumped only if a field below is renamed or removed. |
| `generated_at` | When the analysis ran (ISO-8601 UTC). The page reloads when this changes. |
| `sources` | Which diaries were read, how many lines, runs found / kept. |
| `diary_check` | `{status, issues[]}` for the server diary as a whole. |
| `slot_order` | Slot keys in display order. |
| `slots` | `{slot_key: slot}` — one per test setup, newest run wins. |
| `comparisons` | Cross-run series: `e1_vs_n`, `e2_vs_n`, `overhead_vs_classical`. |
| `e4` | `{limits, rows[], notes[]}` — artifact sizes (not tied to a run). |
| `external_nodes` | Chargers the tester did not start: the Pi (`kind: "hardware"`, CP0100) and E6 clients. |

## A slot

Key: `experiment|n<N>|<mode>|tls` or `...|plain` — e.g. `e2|n500|pqc|tls`.

| Key | What it is |
|---|---|
| `experiment`, `n_stations`, `crypto_mode`, `tls` | The setup. |
| `harness_run_id`, `server_run_ids[]` | The tester's run id; every server run id seen (several after an E2 restart). |
| `started_at`, `finished_at`, `wall_s`, `completed` | Timing; `completed=false` means the run was stopped early. |
| `trust` | `{status: pass/warn/fail, issues[{level, code, message}]}`. **Show it next to every number.** |
| `overview` | `stations`, `connections`, `sessions`, `meter`, `offline_queue`, and `timeline` (`connected`, `charging`, `power_w` as `[[seconds, value], ...]`). |
| `e1` | `station_connect_ms` (headline), `reconnect_connect_ms`, `server_upgrade_ms`, `bytes_per_connection` — each a *distribution* (below) — plus `tls`. |
| `e2` | `null` unless the server restarted in the run. Else `t50_s`, `t95_s`, `t100_s` (`null` = never reached), `population`, `recovered`, `unrecovered`, `curve`, `snapshot_curve`, `integrity{...}`. |
| `e3` | `null` unless a migration happened. Else `final_counts`, `state_timeline{state: [[s, count]]}`, `waves[]`, `markers[]`, `rollbacks`, `duration_s`, `charging{...}`. |
| `e5` | `identity_checks`, `identity_rejected`, `identity_rejections[]`, `connection_failures{}`, `callerrors`, `commands_received`. |

## A distribution

`{n, mean, stdev, min, median, median_ci95: [low, high], p90, p95, p99, max,
box: {whisker_low, q1, median, q3, whisker_high, outliers[]}, ecdf: [[value, percent], ...]}`
— or just `{n: 0}` when there is no data. Times are milliseconds unless the key ends in `_s`.

## Rules for readers

- Never recompute a number the file already has; format it. (Same rule as Track A's
  `aggregate_power_w`: one place computes, everyone else displays.)
- `null` means "not measured / not reached". Never show it as `0`.
- Modes are `classical`, `hybrid`, `pqc`. Colours: blue, orange, aqua — the same on every page.
