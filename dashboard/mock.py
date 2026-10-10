"""
A pretend CSMS for building and rehearsing the dashboard without a server.

Track C (dashboard). Phase C-F6 (FINAL 2-DAY PLAN).

--------------------------------------------------------------------
IN PLAIN WORDS

The dashboard reads the CSMS's `/api`. Until Track A's dashboard API (A-F4)
is on `main`, and whenever you want to rehearse without starting 50
chargers, this file plays the server: a fleet of 49 simulated chargers plus
the Raspberry Pi (CP-PI01), all charging, answering the same GET endpoints
with the same JSON shapes:

    /api/health                    + "mock": true, so the page shows MOCK
    /api/fleet                     Contract 6 FleetSnapshot (+ the A-F4 fields)
    /api/migration                 Contract 4 MigrationStatus (+ "kind")
    /api/events?after=&limit=      A-F4's event feed: {events, last_seq}
    /api/migration/start           canary -> waves -> threshold -> rollback/halt
    /api/migration/rollback        manual rollback of a wave
    /api/migration/rotate          key rotation (old key valid until the new one passes)
    /api/fleet/limit, clear-limit  fleet power limit (the Pi's "LED" dims)
    /api/mock/storm                every charger reconnects and re-proves its key
    /api/mock/impostor             a stolen certificate without the ML-DSA key

NOTHING HERE IS A RESULT. Times and key ids are invented. The page shows a
large "MOCK" badge whenever /api/health says mock, so a screenshot of it can
never be mistaken for a measurement.

--------------------------------------------------------------------
SHAPES (kept identical to the real server, so the page needs no mock mode)

    station    csms/fleet.py StationView.to_dict(), plus the fields A-F4 adds:
               tls_cipher, identity_ok, pq_key_id, last_pq_check
               {at, result, trigger, duration_ms}; the Pi adds measured_mw
               (NOT yet sent by the real Pi -- see limitations.md L37).
    events     Contract 3 Event dicts (timestamp, event_type, station_id,
               outcome, payload, ...) plus `seq`. Track B's lines keep the
               charger in payload.station, exactly as on the real server.

`clock` is injectable so tests can step time instead of sleeping.
"""

from __future__ import annotations

import hashlib
import random
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable

ALGORITHM = "ML-DSA-44"
MAX_POWER_W = 7400.0
PI_ID = "CP-PI01"
PI_MAX_MW = 25.0              # what the LED circuit draws at full brightness (made up)
STEP_S = 0.25                 # one charger handled per step during a wave
FAILURE_THRESHOLD = 0.2
EVENT_BUFFER = 2000


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()


def _key_id(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()[:16]


class MockFleet:
    """The whole pretend server. Thread-safe: every public method locks."""

    def __init__(self, *, n: int = 49, mode: str = "hybrid", tls: bool = True,
                 halt: bool = False, seed: int = 7,
                 clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self.mode = mode
        self.tls = tls
        self.rng = random.Random(seed)
        self.lock = threading.RLock()
        self.run_id = "mock" + _key_id(str(seed))[:8]
        self.started = clock()
        self.limit_w: float | None = None

        self.events: deque[dict[str, Any]] = deque(maxlen=EVENT_BUFFER)
        self.seq = 0

        ids = [f"CP{i:04d}" for i in range(1, n + 1)] + [PI_ID]
        self.legacy = {f"CP{i:04d}" for i in range(41, 46)}
        self.refusers = {f"CP{i:04d}" for i in range(21, 26)} if halt else set()
        self.stations: dict[str, dict[str, Any]] = {}
        for k, sid in enumerate(ids):
            self.stations[sid] = self._new_station(sid, k)
            self._emit("connection_established", sid, outcome="success",
                       handshake_scope="server_upgrade",
                       tls_version="TLSv1.3" if tls else None)
            self._emit("state_changed", sid, outcome="success", transition="booted")
        self.keys: dict[str, str] = {}            # enrolled key_id per charger

        self.status = self._idle_status()
        self._plan: list[dict[str, Any]] = []      # waves still to run
        self._cursor = 0                           # next station in the current wave
        self._next_step = 0.0
        self._wave_start = 0.0
        self._storm_until: float | None = None
        self._storm_back: dict[str, float] = {}
        self._impostor_until: float | None = None
        self._impostor_next = 0.0

    # =================================================================
    # state
    # =================================================================

    def _new_station(self, sid: str, k: int) -> dict[str, Any]:
        now = self.clock()
        supported = ["ECDSA-P256"] if sid in self.legacy else ["ECDSA-P256", ALGORITHM]
        st = {
            "station_id": sid,
            "connection_state": "connected",
            "boot_accepted": True,
            "pq_verified": False,
            "pq_check_required": False,
            "connected_since": _iso(now),
            "last_seen_at": _iso(now),
            "last_heartbeat_at": _iso(now),
            "seconds_since_heartbeat": 1.0,
            "last_handshake_ms": round(0.3 + self.rng.random() * 0.2, 3),
            "ocpp_status": "Occupied",
            "charging_state": "Charging",
            "active_transaction_id": f"tx-{sid}",
            "power_w": MAX_POWER_W,
            "energy_wh": 0.0,
            "bytes_tx": 0, "bytes_rx": 0,
            "tls_version": "TLSv1.3" if self.tls else None,
            "tls_cipher": "TLS_AES_256_GCM_SHA384" if self.tls else None,
            "peer_cert_bytes": 612 if self.tls else None,
            "identity_ok": True if self.tls else None,
            "power_is_stale": False,
            "current_algorithm": "ECDSA-P256",
            "supported_algorithms": supported,
            "certificate_serial": _key_id("cert" + sid)[:12],
            "certificate_expiry": None,
            "previous_certificate_serial": None,
            "migration_wave": None,
            "migration_state": "pending",
            "pq_key_id": None,
            "last_pq_check": None,
        }
        if sid == PI_ID:
            st["measured_mw"] = PI_MAX_MW
            st["power_source"] = "scaled"
        return st

    def _idle_status(self, kind: str = "migration") -> dict[str, Any]:
        return {
            "migration_id": "", "kind": kind, "target_mode": self.mode,
            "phase": "idle", "total_stations": len(self.stations),
            "pending": len(self.stations), "in_progress": 0, "migrated": 0,
            "rolled_back": 0, "incompatible": 0, "current_wave": None,
            "total_waves": 0, "waves": [], "started_at": None, "completed_at": None,
        }

    def _emit(self, event_type: str, station_id: str | None = None, *,
              outcome: str | None = None, **payload: Any) -> None:
        self.seq += 1
        self.events.append({
            "seq": self.seq, "timestamp": _iso(self.clock()),
            "monotonic_ns": int(self.clock() * 1e9), "event_type": event_type,
            "run_id": self.run_id, "crypto_mode": self.mode,
            "station_id": station_id, "handshake_ms": None, "bytes_tx": None,
            "bytes_rx": None, "outcome": outcome, "payload": payload,
        })

    def _pq_check(self, sid: str, ok: bool, trigger: str, *, key_id: str | None,
                  wave_id: int | None = None, detail: str | None = None) -> float:
        ms = round(25 + self.rng.random() * 40, 1)
        result = "success" if ok else "rejected"
        payload = dict(transition="pq_auth", station=sid, result=result,
                       detail=detail or ("signature verified" if ok else "signature did not verify"),
                       duration_ms=ms, algorithm=ALGORITHM, key_id=key_id, trigger=trigger,
                       source="orchestrator" if trigger in ("migration", "rotation") else "boot_verifier")
        if wave_id is not None:
            payload["wave_id"] = wave_id
        self._emit("connection_attempt", None, **payload)
        st = self.stations[sid]
        st["last_pq_check"] = {"at": _iso(self.clock()), "result": result,
                               "trigger": trigger, "duration_ms": ms}
        if ok:
            st["pq_verified"] = True
        return ms

    def _recount(self) -> None:
        s = self.status
        if s["kind"] == "rotation" and s["phase"] != "idle":
            return  # rotation counts are kept by the rotation itself (_finish_wave)
        for k in ("pending", "in_progress", "migrated", "rolled_back", "incompatible"):
            s[k] = 0
        for st in self.stations.values():
            s[st["migration_state"]] = s.get(st["migration_state"], 0) + 1

    # =================================================================
    # the API
    # =================================================================

    def health(self) -> dict[str, Any]:
        with self.lock:
            return {"ok": True, "mock": True, "run_id": self.run_id, "crypto_mode": self.mode,
                    "tls": self.tls, "identity_check": "enforce" if self.tls else "off",
                    "pq_algorithm": ALGORITHM,
                    "migration": f"MOCK orchestrator ({ALGORITHM}, made-up timings)"}

    def fleet(self) -> dict[str, Any]:
        with self.lock:
            self.tick()
            stations = [dict(s) for s in self.stations.values()]
            connected = [s for s in stations if s["connection_state"] == "connected"]
            charging = [s for s in connected if s["charging_state"] == "Charging"]
            return {
                "generated_at": _iso(self.clock()), "run_id": self.run_id,
                "crypto_mode": self.mode, "total_stations": len(stations),
                "connected_count": len(connected),
                "booted_count": sum(1 for s in connected if s["boot_accepted"]),
                "charging_count": len(charging),
                "aggregate_power_w": round(sum(s["power_w"] or 0.0 for s in charging), 1),
                "stations": stations, "migration": dict(self.status),
            }

    def migration(self) -> dict[str, Any]:
        with self.lock:
            self.tick()
            return dict(self.status)

    def events_after(self, after: int = 0, limit: int = 500) -> dict[str, Any]:
        with self.lock:
            self.tick()
            out = [e for e in self.events if e["seq"] > after][:max(1, min(limit, EVENT_BUFFER))]
            return {"events": out, "last_seq": self.seq}

    # -- controls -----------------------------------------------------------

    def start(self, wave_size: int = 10, canary_count: int = 5,
              target_mode: str = "hybrid", kind: str = "migration") -> tuple[int, dict[str, Any]]:
        with self.lock:
            self.tick()
            if self.status["phase"] in ("canary", "running"):
                return 409, {"error": "a migration or rotation is already running"}
            if kind == "migration" and target_mode not in ("hybrid",):
                return 400, {"error": f"target_mode {target_mode!r} not supported; "
                                      "use hybrid (pure PQC not built, L17)"}
            if kind == "migration":
                candidates = [sid for sid, st in self.stations.items()
                              if st["migration_state"] == "pending"]
            else:
                candidates = [sid for sid, st in self.stations.items()
                              if st["migration_state"] == "migrated"]
            if not candidates:
                return 409, {"error": f"nothing to {'rotate' if kind == 'rotation' else 'migrate'}"}
            waves: list[list[str]] = [candidates[:max(1, canary_count)]]
            rest = candidates[max(1, canary_count):]
            waves += [rest[i:i + max(1, wave_size)] for i in range(0, len(rest), max(1, wave_size))]
            mid = _key_id(f"{kind}{self.clock()}")[:12]
            now = self.clock()
            self.status = self._idle_status(kind)
            self.status.update({
                "migration_id": mid, "phase": "canary", "started_at": _iso(now),
                "total_waves": len(waves), "current_wave": 0,
                "waves": [{"wave_id": i, "is_canary": i == 0, "station_ids": w,
                           "phase": "queued", "migrated_count": 0, "failed_count": 0,
                           "started_at": None, "completed_at": None}
                          for i, w in enumerate(waves)],
            })
            if kind == "rotation":
                self.status.update(total_stations=len(candidates), pending=len(candidates))
            self._plan = waves
            self._cursor = 0
            self._next_step = now + STEP_S
            self._start_wave(0)
            # Same shape as Track B's B-F2: a rotation run is announced with the
            # ordinary migration lines plus kind "rotation".
            self._rotated: dict[int, list[tuple[str, str | None]]] = {}
            self._emit("migration_started", None, source="orchestrator", migration_id=mid,
                       target_mode=self.mode, total=len(candidates), wave_size=wave_size,
                       canary_count=canary_count, **self._meta())
            self._recount()
            return 200, {"migration_id": mid}

    def rotate(self, wave_size: int = 10, canary_count: int = 5) -> tuple[int, dict[str, Any]]:
        return self.start(wave_size, canary_count, kind="rotation")

    def rollback(self, wave_id: int) -> tuple[int, dict[str, Any]]:
        with self.lock:
            waves = self.status.get("waves") or []
            if not (0 <= wave_id < len(waves)) or waves[wave_id]["phase"] == "queued":
                return 200, {"reverted": False}
            self._rollback_wave(wave_id, trigger="manual")
            return 200, {"reverted": True}

    def set_limit(self, watts: float | None) -> tuple[int, dict[str, Any]]:
        with self.lock:
            self.limit_w = None if watts is None else max(0.0, float(watts))
            targets = 0
            for st in self.stations.values():
                if st["connection_state"] != "connected":
                    continue
                targets += 1
                self._apply_power(st)
            return 200, {"action": "SetChargingProfile" if watts is not None else "ClearChargingProfile",
                         "targets": targets, "ok": targets, "not_ok": 0}

    def storm(self, outage_s: float = 3.0) -> tuple[int, dict[str, Any]]:
        """Every charger loses the server, then reconnects and re-proves its key."""
        with self.lock:
            now = self.clock()
            self._emit("server_stopping", None)
            for sid, st in self.stations.items():
                st.update(connection_state="disconnected", boot_accepted=False, pq_verified=False)
                self._emit("connection_closed", sid, outcome="success", reason="server_restart")
            self._emit("server_started", None)
            ids = list(self.stations)
            self.rng.shuffle(ids)
            self._storm_back = {sid: now + outage_s + 0.08 * k for k, sid in enumerate(ids)}
            self._storm_until = max(self._storm_back.values())
            return 200, {"ok": True, "reconnecting": len(ids)}

    def impostor(self, station_id: str = "CP0003", for_s: float = 15.0) -> tuple[int, dict[str, Any]]:
        """A stolen classical certificate, no ML-DSA key: refused at every boot."""
        with self.lock:
            st = self.stations.get(station_id)
            if st is None or st["migration_state"] != "migrated":
                return 409, {"error": f"{station_id} must be migrated first (run a migration)"}
            self._impostor_sid = station_id
            self._impostor_until = self.clock() + for_s
            self._impostor_next = self.clock()
            return 200, {"ok": True, "station_id": station_id}

    # =================================================================
    # time passes
    # =================================================================

    def tick(self) -> None:
        with self.lock:
            now = self.clock()
            self._tick_energy(now)
            self._tick_waves(now)
            self._tick_storm(now)
            self._tick_impostor(now)
            self._recount()

    def _apply_power(self, st: dict[str, Any]) -> None:
        cap = MAX_POWER_W if self.limit_w is None else min(MAX_POWER_W, self.limit_w)
        charging = st["connection_state"] == "connected" and st["charging_state"] == "Charging"
        st["power_w"] = cap if charging else 0.0
        if st["station_id"] == PI_ID:
            st["measured_mw"] = round(PI_MAX_MW * (st["power_w"] / MAX_POWER_W), 1) if charging else 0.0

    def _tick_energy(self, now: float) -> None:
        last = getattr(self, "_last_energy", now)
        dt_h = max(0.0, now - last) / 3600.0
        self._last_energy = now
        for st in self.stations.values():
            self._apply_power(st)
            st["energy_wh"] = round((st["energy_wh"] or 0.0) + (st["power_w"] or 0.0) * dt_h, 3)
            if st["connection_state"] == "connected":
                st["last_seen_at"] = _iso(now)

    def _meta(self) -> dict[str, Any]:
        """Track B's _run_meta(): kind "rotation" on every line of a rotation run."""
        return {"kind": "rotation"} if self.status.get("kind") == "rotation" else {}

    def _start_wave(self, w: int) -> None:
        wave = self.status["waves"][w]
        wave.update(phase="running", started_at=_iso(self.clock()))
        self.status["current_wave"] = w
        self.status["phase"] = "canary" if w == 0 else "running"
        self._cursor = 0
        self._emit("wave_started", None, source="orchestrator", wave_id=w,
                   is_canary=w == 0, size=len(wave["station_ids"]), **self._meta())

    def _tick_waves(self, now: float) -> None:
        if self.status["phase"] not in ("canary", "running"):
            return
        while now >= self._next_step and self.status["phase"] in ("canary", "running"):
            self._next_step += STEP_S
            w = self.status["current_wave"]
            wave = self.status["waves"][w]
            ids = wave["station_ids"]
            if self._cursor < len(ids):
                self._handle_one(ids[self._cursor], w, wave)
                self._cursor += 1
                continue
            self._finish_wave(w, wave)

    def _handle_one(self, sid: str, w: int, wave: dict[str, Any]) -> None:
        st = self.stations[sid]
        rotation = self.status["kind"] == "rotation"
        if not rotation and ALGORITHM not in st["supported_algorithms"]:
            st["migration_state"] = "incompatible"
            return
        if st["connection_state"] != "connected":
            self._emit("station_deferred", None, source="orchestrator", station=sid, wave_id=w)
            return
        if rotation:
            old = self.keys.get(sid)
            new = _key_id(f"{sid}-rot-{self.clock()}")
            ok = sid not in self.refusers
            self._emit("rotation_started", None, source="orchestrator", station=sid,
                       old_key_id=old, new_key_id=new, wave_id=w)
            self._pq_check(sid, ok, "rotation", key_id=new, wave_id=w)
            if ok:
                self._rotated.setdefault(w, []).append((sid, old))
                self.keys[sid] = new
                st["pq_key_id"] = new
                wave["migrated_count"] += 1
                self._emit("rotation_completed", None, source="orchestrator", station=sid,
                           old_key_id=old, new_key_id=new, wave_id=w, detail="new key proven; old key dropped")
            else:
                wave["failed_count"] += 1
                self._emit("rotation_failed", None, source="orchestrator", station=sid,
                           old_key_id=old, new_key_id=new, wave_id=w, detail="new key not proven; old key kept")
            return
        st["migration_state"] = "in_progress"
        st["migration_wave"] = w
        if sid in self.refusers:
            wave["failed_count"] += 1
            return
        kid = _key_id(f"{sid}-{self.clock()}")
        self._emit("certificate_installed", None, source="orchestrator", transition="pq_enrolled",
                   station=sid, key_id=kid, algorithm=ALGORITHM, trigger="migration", wave_id=w)
        self._pq_check(sid, True, "migration", key_id=kid, wave_id=w)
        self.keys[sid] = kid
        st.update(migration_state="migrated", pq_key_id=kid, pq_check_required=True,
                  current_algorithm=ALGORITHM)
        wave["migrated_count"] += 1

    def _finish_wave(self, w: int, wave: dict[str, Any]) -> None:
        eligible = wave["migrated_count"] + wave["failed_count"]
        failed_share = wave["failed_count"] / eligible if eligible else 0.0
        wave["completed_at"] = _iso(self.clock())
        if self.status["kind"] == "rotation":
            self.status["migrated"] = sum(x["migrated_count"] for x in self.status["waves"])
            self.status["rolled_back"] = sum(x["failed_count"] for x in self.status["waves"])
            self.status["pending"] = max(0, self.status["total_stations"] - self.status["migrated"]
                                         - self.status["rolled_back"])
        if failed_share > FAILURE_THRESHOLD:
            self._rollback_wave(w, trigger="threshold")
            return
        wave["phase"] = "completed"
        self._emit("wave_completed", None, source="orchestrator", wave_id=w,
                   migrated=wave["migrated_count"], failed=wave["failed_count"], **self._meta())
        if w + 1 < len(self.status["waves"]):
            self._start_wave(w + 1)
        else:
            self.status.update(phase="completed", completed_at=_iso(self.clock()), current_wave=None)
            self._emit("migration_completed", None, source="orchestrator",
                       migrated=sum(x["migrated_count"] for x in self.status["waves"]), **self._meta())

    def _rollback_wave(self, w: int, trigger: str) -> None:
        wave = self.status["waves"][w]
        if self.status["kind"] == "rotation":
            # Track B: a failed rotation wave puts the old keys back.
            for sid, old in getattr(self, "_rotated", {}).get(w, []):
                self.keys[sid] = old
                self.stations[sid]["pq_key_id"] = old
        else:
            for sid in wave["station_ids"]:
                st = self.stations[sid]
                if st["migration_state"] in ("in_progress", "migrated"):
                    st.update(migration_state="rolled_back", pq_key_id=None, pq_verified=False,
                              pq_check_required=False, current_algorithm="ECDSA-P256")
                    self.keys.pop(sid, None)
        wave.update(phase="rolled_back", completed_at=wave["completed_at"] or _iso(self.clock()))
        self._emit("wave_rolled_back", None, source="orchestrator" if trigger != "manual" else "server",
                   wave_id=w, trigger=trigger, failed=wave["failed_count"], **self._meta())
        if trigger != "manual":
            self.status.update(phase="rolled_back", completed_at=_iso(self.clock()), current_wave=None)

    def _tick_storm(self, now: float) -> None:
        if self._storm_until is None:
            return
        for sid, back in list(self._storm_back.items()):
            if now < back:
                continue
            st = self.stations[sid]
            st.update(connection_state="connected", boot_accepted=True,
                      connected_since=_iso(back))
            self._emit("connection_established", sid, outcome="success",
                       handshake_scope="server_upgrade",
                       tls_version="TLSv1.3" if self.tls else None)
            self._emit("state_changed", sid, outcome="success", transition="booted")
            if self.mode == "hybrid" and sid in self.keys:
                self._pq_check(sid, True, "boot", key_id=self.keys[sid])
            del self._storm_back[sid]
        if not self._storm_back:
            self._storm_until = None

    def _tick_impostor(self, now: float) -> None:
        if self._impostor_until is None:
            return
        sid = self._impostor_sid
        st = self.stations[sid]
        if now >= self._impostor_until:
            # The real charger comes back with its real key.
            st.update(connection_state="connected", boot_accepted=True)
            self._emit("connection_established", sid, outcome="success")
            self._emit("state_changed", sid, outcome="success", transition="booted")
            self._pq_check(sid, True, "boot", key_id=self.keys.get(sid))
            self._impostor_until = None
            return
        if now < self._impostor_next:
            return
        self._impostor_next = now + 3.0
        st.update(connection_state="connected", boot_accepted=True, pq_verified=False)
        self._emit("connection_established", sid, outcome="success")
        self._emit("state_changed", sid, outcome="success", transition="booted")
        self._pq_check(sid, False, "boot", key_id=self.keys.get(sid),
                       detail="Rejected: station holds no key with this key_id")
        st.update(connection_state="disconnected", boot_accepted=False)
        self._emit("connection_closed", sid, outcome="failure", reason="pq_auth_failed", code=1008)
