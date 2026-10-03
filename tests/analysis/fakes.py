"""
Synthetic diaries for the analysis tests.

Track C (tests). Phase C6.

Builds a server diary (Contract 3 shape, as csms/ writes it) and tester
diaries (harness/timing_log.py shape) for runs whose answers are known in
advance: exact connection times, exact recovery times after a storm, an
exact migration with a rolled-back wave, a rejected identity, and the
Raspberry Pi as an external node. The analysis must reproduce those numbers.

Field names and event names are copied from the real writers:
csms/events.py, csms/registry.py, csms/handlers.py, harness/timing_log.py,
harness/load_generator.py. If one of those changes, these fakes must change
with it -- that is the point of keeping them in one place.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

T0 = 1_790_000_000.0  # a fixed moment, so every test is deterministic


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()


class Diaries:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.logs = self.root / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.server_lines: list[dict[str, Any]] = []
        self.harness_lines: dict[str, list[dict[str, Any]]] = {}

    # -- raw writers ----------------------------------------------------------

    def server(self, t: float, event_type: str, station_id: str | None = None, *,
               run_id: str = "srv1", mode: str = "classical", handshake_ms: float | None = None,
               bytes_tx: int | None = None, bytes_rx: int | None = None,
               outcome: str | None = None, **payload: Any) -> None:
        self.server_lines.append({
            "timestamp": iso(t), "monotonic_ns": int(t * 1e9), "event_type": event_type,
            "run_id": run_id, "crypto_mode": mode, "station_id": station_id,
            "handshake_ms": handshake_ms, "bytes_tx": bytes_tx, "bytes_rx": bytes_rx,
            "outcome": outcome, "payload": payload,
        })

    def harness(self, name: str, t: float, event_type: str, *, run_id: str, experiment: str,
                n: int, mode: str, tls: bool, station_id: str | None = None, **fields: Any) -> None:
        self.harness_lines.setdefault(name, []).append({
            "ts": iso(t), "elapsed_ms": 0.0, "run_id": run_id, "experiment": experiment,
            "n_stations": n, "crypto_mode": mode, "tls": tls, "event_type": event_type,
            "station_id": station_id, **fields,
        })

    # -- a whole run ------------------------------------------------------------

    def fleet_run(self, *, experiment: str, n: int, mode: str, tls: bool = True,
                  start: float = T0, run_id: str = "run1", server_run_id: str = "srv1",
                  connect_ms: list[float] | None = None, charge_s: int = 5,
                  storm: dict[str, Any] | None = None, migrate: bool = False,
                  crash: str | None = None, omit_connect_ms: bool = False,
                  first_id: int = 1, pq_checks: bool = True, bad_counts: bool = False,
                  agent_keys: dict[str, bool] | None = None,
                  connected_lines: bool = False, stop_early: bool = False) -> dict[str, Any]:
        """
        A run of n chargers: connect, boot, charge for charge_s seconds with a
        7.4 kW reading each second, disconnect. Optional storm / migration.
        Returns the known answers.

        C6.2: connected_lines=True also writes a station_connected line per
        connection (as the load generator does since C6.2); stop_early=True
        drops every station_finished row and the run_finished row -- the
        tester stopped (Ctrl-C) before the chargers reported.
        """
        name = f"{experiment}_n{n}_{mode}.jsonl"
        ids = [f"CP{i:04d}" for i in range(first_id, first_id + n)]
        connect_ms = connect_ms or [10.0 + i for i in range(n)]
        H = dict(run_id=run_id, experiment=experiment, n=n, mode=mode, tls=tls)
        S = dict(mode=mode)

        self.harness(name, start, "run_started", config={"crypto_mode": mode}, **H)
        srv = server_run_id
        storm_restart = None
        recovery = storm.get("recovery", []) if storm else []

        for i, sid in enumerate(ids):
            t = start + 0.1 + i * 0.01
            self.harness(name, t, "station_spawned", station_id=sid, **H)
            self.server(t + 0.01, "connection_established", sid, run_id=srv, handshake_ms=0.5,
                        outcome="success", handshake_scope="server_upgrade",
                        tls_version="TLSv1.3" if tls else None, **S)
            self.server(t + 0.02, "state_changed", sid, run_id=srv, outcome="success", transition="booted", **S)
            self.server(t + 0.03, "transaction_started", sid, run_id=srv, outcome="success",
                        transaction_id=f"tx{sid}", seq_no=0, power_w=7400.0, energy_wh=0.0,
                        offline=False, applied_to_live_state=True, **S)

        if storm:
            kill = start + storm["kill_at"]
            storm_restart = kill + storm["outage"]
            self.harness(name, kill, "storm_kill", detected_by="supervisor", **H)
            self.server(storm_restart, "server_started", run_id="srv2", **S)
            self.harness(name, storm_restart + 0.05, "storm_restart", detected_by="supervisor", **H)

        for sec in range(1, charge_s + 1):
            for i, sid in enumerate(ids):
                t = start + 0.1 + i * 0.01 + sec
                run_now = srv
                if storm:
                    back = recovery[i] if i < len(recovery) and recovery[i] is not None else 1e9
                    if kill <= t < storm_restart + back:
                        continue  # the server is down or the charger has not reconnected
                    if t >= storm_restart:
                        run_now = "srv2"
                self.server(t, "transaction_updated", sid, run_id=run_now, outcome="success",
                            transaction_id=f"tx{sid}", seq_no=sec, power_w=7400.0,
                            energy_wh=7400.0 * sec / 3600.0, offline=False,
                            applied_to_live_state=True, **S)

        if storm:
            for i, sid in enumerate(ids):
                if i >= len(recovery) or recovery[i] is None:
                    continue
                back = storm_restart + recovery[i]
                self.server(back - 0.005, "connection_established", sid, run_id="srv2", handshake_ms=0.6,
                            outcome="success", handshake_scope="server_upgrade", **S)
                self.server(back, "state_changed", sid, run_id="srv2", outcome="success", transition="booted", **S)
                self.server(back + 0.001, "transaction_updated", sid, run_id="srv2", outcome="success",
                            transaction_id=f"tx{sid}", seq_no=90, power_w=7400.0, energy_wh=1.0,
                            offline=True, applied_to_live_state=False, **S)
            gap_sid = ids[0]
            self.server(storm_restart + recovery[0] + 0.002, "transaction_updated", gap_sid, run_id="srv2",
                        outcome="failure", transition="sequence_gap", transaction_id=f"tx{gap_sid}",
                        missing_events=2, loss_site="in_transit", offline=False, **S)

        end = start + charge_s + 1.0
        final_srv = "srv2" if storm else srv
        for i, sid in enumerate(ids):
            t = end + i * 0.01
            if storm and (i >= len(recovery) or recovery[i] is None):
                pass  # never came back: no end, no close on the new server
            else:
                self.server(t, "transaction_ended", sid, run_id=final_srv, outcome="success",
                            transaction_id=f"tx{sid}", seq_no=charge_s + 1, power_w=0.0,
                            energy_wh=7400.0 * charge_s / 3600.0, offline=False,
                            applied_to_live_state=True, trigger_reason="StopAuthorized", **S)
                self.server(t + 0.005, "connection_closed", sid, run_id=final_srv, outcome="success",
                            bytes_tx=500 + i, bytes_rx=3000 + i, session_duration_ms=6000.0, **S)
            times = [connect_ms[i]]
            if storm and i < len(recovery) and recovery[i] is not None:
                times.append(connect_ms[i] * 3)
            row: dict[str, Any] = dict(
                ok=not (storm and (i >= len(recovery) or recovery[i] is None)),
                crashed=False, error="", connection_attempts=len(times),
                reconnections=len(times) - 1, connect_timeouts=0, total_downtime_s=0.0,
                callerrors=0, commands_received=0, state_transitions=4, offline_queued=1 if storm else 0,
                offline_replayed=1 if storm else 0, offline_dropped=0, wall_s=charge_s + 1.0,
            )
            if not omit_connect_ms:
                row["connect_ms"] = times
            if agent_keys is not None:
                # C6.1: the charger's own post-quantum view (harness StationOutcome).
                row["pq_key_installed"] = bool(agent_keys.get(sid, False))
                row["pq_installs"] = 1 if agent_keys.get(sid) else 0
                row["pq_challenges_signed"] = 1 if agent_keys.get(sid) else 0
            if connected_lines:
                for k, ms in enumerate(times, start=1):
                    self.harness(name, start + 0.1 + i * 0.01 + 0.005 * k, "station_connected",
                                 station_id=sid, connect_ms=ms, connection=k, attempt=k, **H)
            if stop_early:
                continue
            kind = "station_crashed" if sid == crash else "station_finished"
            self.harness(name, t + 0.01, kind, station_id=sid, **row, **H)

        if migrate:
            self._migration(name, ids, start, H, S, srv, pq_checks=pq_checks, bad_counts=bad_counts)

        if not stop_early:
            self.harness(name, end + 1.0, "run_finished", succeeded=n, failed=0, crashed=0, **H)
        return {"ids": ids, "name": name, "storm_restart": storm_restart}

    def _migration(self, name: str, ids: list[str], start: float, H: dict, S: dict, srv: str,
                   *, pq_checks: bool = True, bad_counts: bool = False) -> None:
        """
        Canary (2) -> wave 1 (next 2, succeeds) -> wave 2 (rest, fails at
        install -> rolled back, migration halts). Shaped exactly as the real
        orchestrator writes it through Track A's emitter (main 8a5922a):
        top-level station_id None, `source: "orchestrator"`, per-charger
        lines carrying `station` and `result` in the payload, and NO
        migration_completed after a rollback (the orchestrator halts).

        pq_checks=False -> the server was not wired to challenge ("key
        installed" only). bad_counts=True -> the controller's counts do not
        add up (to test the trust check).
        """
        canary, w1, w2 = ids[:2], ids[2:4], ids[4:]
        began = start + 1.0
        O = dict(source="orchestrator", **S)
        self.server(began, "migration_started", None, run_id=srv, migration_id="m1",
                    target_mode="pqc", total=len(ids), wave_size=2, canary_count=2, **O)
        self.server(began, "wave_started", None, run_id=srv, wave_id=0, is_canary=True, size=2, **O)
        if pq_checks:
            for k, sid in enumerate(canary):
                self.server(began + 0.4 + 0.01 * k, "connection_attempt", None, run_id=srv,
                            transition="pq_auth", station=sid, wave_id=0, result="success",
                            detail="signature verified", duration_ms=40.0 + k, algorithm="ML-DSA-44", **O)
        self.server(began + 1.0, "wave_completed", None, run_id=srv, wave_id=0,
                    migrated=2, failed=0, incompatible=0, deferred=0, **O)
        self.server(began + 1.0, "wave_started", None, run_id=srv, wave_id=1, is_canary=False, size=2, **O)
        if pq_checks:
            for k, sid in enumerate(w1):
                self.server(began + 1.4 + 0.01 * k, "connection_attempt", None, run_id=srv,
                            transition="pq_auth", station=sid, wave_id=1, result="success",
                            detail="signature verified", duration_ms=50.0 + k, algorithm="ML-DSA-44", **O)
        self.server(began + 2.0, "wave_completed", None, run_id=srv, wave_id=1,
                    migrated=2, failed=0, incompatible=0, deferred=0, **O)
        self.server(began + 2.0, "wave_started", None, run_id=srv, wave_id=2, is_canary=False,
                    size=len(w2), **O)
        self.server(began + 3.0, "wave_rolled_back", None, run_id=srv, wave_id=2,
                    migrated=0, failed=len(w2), eligible=len(w2), deferred=0, **O)

        def state_at(sid: str, t: float) -> str:
            rel = t - began
            if sid in canary:
                return "pending" if rel < 0 else "in_progress" if rel < 1 else "migrated"
            if sid in w1:
                return "pending" if rel < 1 else "in_progress" if rel < 2 else "migrated"
            return "pending" if rel < 2 else "in_progress" if rel < 3 else "rolled_back"

        for k in range(0, 7):
            t = start + 0.5 + k
            phase = "idle" if t < began else ("rolled_back" if t >= began + 3.0 else "running")
            waves = [
                {"wave_id": 0, "is_canary": True, "station_ids": canary, "phase": "completed",
                 "migrated_count": 2, "failed_count": 0, "started_at": iso(began), "completed_at": iso(began + 1)},
                {"wave_id": 1, "is_canary": False, "station_ids": w1, "phase": "completed",
                 "migrated_count": 2, "failed_count": 0, "started_at": iso(began + 1), "completed_at": iso(began + 2)},
                {"wave_id": 2, "is_canary": False, "station_ids": w2, "phase": "rolled_back",
                 "migrated_count": 0, "failed_count": len(w2), "started_at": iso(began + 2), "completed_at": iso(began + 3)},
            ]
            states = {sid: state_at(sid, t) for sid in ids}
            counts = {st: sum(1 for v in states.values() if v == st)
                      for st in ("pending", "in_progress", "migrated", "rolled_back", "incompatible")}
            if bad_counts and phase == "rolled_back":
                counts["rolled_back"] = 0          # the pre-Day-12 counter bug
            snapshot = {
                "run_id": srv, "crypto_mode": S["mode"],
                "stations": [
                    {"station_id": sid, "connection_state": "connected", "boot_accepted": True,
                     "migration_state": states[sid]}
                    for sid in ids
                ],
                "migration": {"phase": phase, "target_mode": "pqc", "waves": waves,
                              "total_stations": len(ids), **counts},
            }
            self.harness(name, t, "fleet_snapshot", snapshot=snapshot, **H)

    # -- output -------------------------------------------------------------------

    def write(self, *, truncate_server: bool = False) -> tuple[Path, Path]:
        events = self.logs / "events.jsonl"
        lines = sorted(self.server_lines, key=lambda e: e["timestamp"])
        text = "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in lines)
        if truncate_server:
            text += '{"timestamp":"2026-01-01T00:00:00+00:00","event_ty'
        events.write_text(text, encoding="utf-8")
        for name, rows in self.harness_lines.items():
            rows = sorted(rows, key=lambda r: r["ts"])
            (self.logs / name).write_text(
                "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
            )
        return events, self.logs
