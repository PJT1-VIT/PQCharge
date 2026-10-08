"""
Seam tests: the analysis against the OTHER tracks' real code, not copies.

Track C (tests). Phase C6.1, for the A+B+C integration session.

The other analysis tests use hand-made diaries shaped like the real ones.
These use the real thing, so if Track A or Track B ever change what they
write, a test here fails at once instead of the live session going quietly
wrong:

    T2  Track A's real EventLog + real orchestrator_emitter (csms/migration.py)
        -> C6 finds the charger in payload.station and the result in
        payload.result
    T3  Track A's real FleetSnapshot / StationView, carrying Track B's real
        MigrationStatus -> C6's E3 reads every state and the counts
    END-TO-END  Track B's real MigrationOrchestrator, with real ML-DSA, real
        wire messages (agent/pqc_messages.py) and Track C's real charger
        handlers (ChargingStation via StationClient.on_data_transfer),
        writing through Track A's real emitter. A Stage-6-shaped fleet:
        canary, waves, a legacy charger (incompatible), an offline charger
        (deferred), and two refusers the tester did not start (rolled back).
        C6 must report exactly what the orchestrator itself reports.

The end-to-end test needs the post-quantum library (cryptography>=50) and skips
cleanly without it, like the other post-quantum tests.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import pytest

from analysis import run
from tests.analysis.fakes import Diaries

migration_mod = pytest.importorskip("csms.migration", reason="Track A csms/migration.py")
from csms.events import EventLog  # noqa: E402
from csms.fleet import FleetSnapshot, StationView  # noqa: E402
from idmanager.api import MigrationPhase, MigrationStatus, WavePhase, WaveStatus  # noqa: E402


def _tester_run(d: Diaries, name: str, ids: list[str], t0: float, *, run_id: str = "seam") -> dict:
    H = dict(run_id=run_id, experiment=name.split("_n")[0], n=len(ids), mode="pqc", tls=False)
    d.harness(name, t0, "run_started", config={}, **H)
    for i, sid in enumerate(ids):
        d.harness(name, t0 + 0.01 * (i + 1), "station_spawned", station_id=sid, **H)
    return H


def _server_station_lines(log: EventLog, ids: list[str]) -> None:
    """What Track A's registry/handlers write when chargers connect and charge."""
    for sid in ids:
        log.emit("connection_established", sid, handshake_ms=0.5, outcome="success",
                 handshake_scope="server_upgrade")
        log.emit("state_changed", sid, outcome="success", transition="booted")
        log.emit("transaction_started", sid, outcome="success", transaction_id=f"tx{sid}",
                 seq_no=0, power_w=7400.0, energy_wh=0.0, offline=False,
                 applied_to_live_state=True)


# =====================================================================
# T2 — Track A's real emitter
# =====================================================================


def test_t2_the_real_orchestrator_emitter_is_read_correctly(tmp_path):
    d = Diaries(tmp_path)
    ids = ["CP0001", "CP0002"]
    t0 = time.time() - 1.0
    H = _tester_run(d, "t2_n2_pqc.jsonl", ids, t0)

    log = EventLog(path=d.logs / "t2_events.jsonl", run_id="srvT2", crypto_mode="pqc")
    _server_station_lines(log, ids)
    emit = migration_mod.orchestrator_emitter(log)
    # Exactly the calls idmanager/orchestrator.py makes (main 8a5922a):
    emit("migration_started", migration_id="m", target_mode="pqc", total=2,
         wave_size=1, canary_count=1)
    emit("wave_started", wave_id=0, is_canary=True, size=1)
    emit("connection_attempt", transition="pq_auth", station="CP0001", wave_id=0,
         result="success", detail="signature verified", duration_ms=12.5,
         algorithm="ML-DSA-44")
    emit("wave_completed", wave_id=0, migrated=1, failed=0, incompatible=0, deferred=0)
    emit("wave_started", wave_id=1, is_canary=False, size=1)
    emit("station_deferred", station="CP0002", wave_id=1, reason="not connected")
    emit("wave_completed", wave_id=1, migrated=0, failed=0, incompatible=0, deferred=1)
    emit("migration_completed", migrated=1, incompatible=0)
    log.close()
    d.harness("t2_n2_pqc.jsonl", time.time() + 0.1, "run_finished", **H)
    d.write()

    results = run.build(None, d.logs)
    slot = results["slots"]["t2|n2|pqc|plain"]
    e3, e5 = slot["e3"], slot["e5"]
    assert e3["pq_checks"]["passed"] == 1 and e3["pq_checks"]["algorithm"] == "ML-DSA-44"
    assert e3["pq_checks"]["round_trip_ms"]["median"] == 12.5
    assert e3["deferred"] == {"count": 1, "station_ids": ["CP0002"]}
    assert e5["pq_checks"] == 1 and e5["pq_passed"] == 1
    assert results["sources"]["server_lines_normalised"] == 2
    assert [m["event"] for m in e3["markers"]] == [
        "migration_started", "wave_started", "wave_completed",
        "wave_started", "wave_completed", "migration_completed",
    ]


# =====================================================================
# T3 — Track A's real snapshot classes, carrying Track B's real status
# =====================================================================


def _snapshot(states: dict[str, str], status: MigrationStatus, connected: set[str]) -> dict:
    views = [
        StationView(station_id=sid, migration_state=st,
                    connection_state="connected" if sid in connected else "disconnected",
                    boot_accepted=sid in connected)
        for sid, st in sorted(states.items())
    ]
    return FleetSnapshot(
        generated_at=datetime.now(timezone.utc), run_id="srv", crypto_mode="pqc",
        stations=views, migration=status.to_dict(),
    ).to_dict()


def test_t3_the_real_fleet_snapshot_is_read_correctly(tmp_path):
    d = Diaries(tmp_path)
    ids = ["CP0001", "CP0002", "CP0003"]
    t0 = time.time() - 2.0
    H = _tester_run(d, "t3_n3_pqc.jsonl", ids, t0)
    log = EventLog(path=d.logs / "t3_events.jsonl", run_id="srvT3", crypto_mode="pqc")
    _server_station_lines(log, ids)
    log.close()

    now = datetime.now(timezone.utc)
    waves = [WaveStatus(wave_id=0, is_canary=True, station_ids=["CP0001"],
                        phase=WavePhase.COMPLETED, migrated_count=1, started_at=now,
                        completed_at=now),
             WaveStatus(wave_id=1, is_canary=False, station_ids=["CP0002", "CP0003", "EXT01"],
                        phase=WavePhase.ROLLED_BACK, failed_count=1, started_at=now,
                        completed_at=now)]
    status = MigrationStatus(migration_id="m", target_mode="pqc", phase=MigrationPhase.ROLLED_BACK,
                             total_stations=4, migrated=1, rolled_back=1, incompatible=1, pending=1,
                             waves=waves)
    states = {"CP0001": "migrated", "CP0002": "incompatible", "CP0003": "pending", "EXT01": "rolled_back"}
    d.harness("t3_n3_pqc.jsonl", time.time() - 0.5, "fleet_snapshot",
              snapshot=_snapshot(states, status, set(ids)), **H)
    d.harness("t3_n3_pqc.jsonl", time.time(), "run_finished", **H)
    d.write()

    e3 = run.build(None, d.logs)["slots"]["t3|n3|pqc|plain"]["e3"]
    assert e3["final_counts"] == {"pending": 1, "in_progress": 0, "migrated": 1,
                                  "rolled_back": 1, "incompatible": 1}
    assert e3["fleet_size"] == 4 and e3["tester_chargers"] == 3
    assert e3["controller_counts"]["total_stations"] == 4
    assert e3["controller_sum_violations"] == 0
    assert [w["phase"] for w in e3["waves"]] == ["completed", "rolled_back"]
    assert e3["rollbacks"] == 1


# =====================================================================
# END TO END — Track B's real orchestrator, real ML-DSA, Track C's real
# charger handlers, Track A's real emitter, then C6.
# =====================================================================


class _Result:
    """The fields the orchestrator reads from a Track A DispatchResult."""

    def __init__(self, ok: bool, response=None, duration_ms: float | None = None):
        self.ok = ok
        self.outcome = "success" if ok else "rejected"
        self.status = getattr(response, "status", None)
        self.response = response
        self.duration_ms = duration_ms


class _Dispatcher:
    """
    Routes each OCPP call to a REAL ChargingStation through its REAL
    StationClient.on_data_transfer -- the code path a live DataTransfer
    takes. Chargers in `refusers` behave like Track A's fake_station: a
    legacy charger that answers UnknownVendorId.
    """

    def __init__(self, clients: dict, refusers: set[str]):
        self.clients = clients
        self.refusers = refusers

    async def send(self, station_id, request, *, timeout_s=None):
        from ocpp.v201 import call_result

        started = time.monotonic()
        await asyncio.sleep(0)
        if station_id in self.refusers:
            response = call_result.DataTransfer(status="UnknownVendorId")
        else:
            response = await self.clients[station_id].on_data_transfer(
                vendor_id=request.vendor_id, message_id=request.message_id, data=request.data,
            )
        ms = (time.monotonic() - started) * 1000.0
        return _Result(ok=response.status == "Accepted", response=response, duration_ms=ms)


class _Fleet:
    """Track B's FleetLike over plain identities, with liveness (skip_offline)."""

    def __init__(self, caps: dict[str, list[str]], connected: set[str]):
        from crypto.identity import StationIdentity

        self.ids = {
            sid: StationIdentity(station_id=sid, current_algorithm="ECDSA-P256",
                                 supported_algorithms=list(algs))
            for sid, algs in caps.items()
        }
        self.connected = connected

    def migration_candidate_ids(self):
        return sorted(self.ids)

    def supported_algorithms(self, sid):
        return list(self.ids[sid].supported_algorithms)

    def set_state(self, sid, state, wave):
        self.ids[sid].migration_state = state
        self.ids[sid].migration_wave = wave

    def mark_migrated_algorithm(self, sid, alg):
        self.ids[sid].current_algorithm = alg

    def is_connected(self, sid):
        return sid in self.connected

    def states(self) -> dict[str, str]:
        return {sid: it.migration_state.value for sid, it in self.ids.items()}


@pytest.mark.asyncio
async def test_end_to_end_stage6_shape_with_every_track_real(tmp_path):
    pytest.importorskip("cryptography.hazmat.primitives.asymmetric.mldsa", reason="PQ backend needs cryptography>=50; see requirements.txt")
    from agent.client import StationClient
    from agent.config import AgentConfig
    from agent.pqc_messages import build_challenge_message, build_install_message, parse_signature
    from agent.station import ChargingStation
    from crypto.pq import PQProvider
    from crypto.pq_auth import PQAuthenticator
    from idmanager.orchestrator import MigrationOrchestrator

    ours = [f"CP000{i}" for i in range(1, 8)]              # the tester's 7 chargers
    capable = ["ECDSA-P256", "ML-DSA-44"]
    caps = {sid: capable for sid in ours}
    caps["CP0007"] = ["ECDSA-P256"]                       # legacy -> incompatible
    caps["CP0008"] = capable                              # in the profile, never connected
    caps["EXT01"] = capable                               # Track A refusers, not ours
    caps["EXT02"] = capable
    connected = set(ours) | {"EXT01", "EXT02"}

    stations = {sid: ChargingStation(AgentConfig(station_id=sid, csms_url="ws://localhost:9"))
                for sid in ours}
    clients = {sid: StationClient(sid, connection=None, commands=st) for sid, st in stations.items()}

    d = Diaries(tmp_path)
    t0 = time.time() - 1.0
    H = _tester_run(d, "stage6_n7_pqc.jsonl", ours, t0)
    log = EventLog(path=d.logs / "stage6_events.jsonl", run_id="srvE2E", crypto_mode="pqc")
    _server_station_lines(log, ours)

    fleet = _Fleet(caps, connected)
    d.harness("stage6_n7_pqc.jsonl", time.time(), "fleet_snapshot",
              snapshot=_snapshot(fleet.states(), MigrationStatus(
                  migration_id="", target_mode="pqc", phase=MigrationPhase.IDLE), connected), **H)

    provider = PQProvider()
    orch = MigrationOrchestrator(
        dispatcher=_Dispatcher(clients, refusers={"EXT01", "EXT02"}),
        authenticator=PQAuthenticator(provider),
        fleet=fleet,
        keypair_factory=provider.generate_keypair,
        install_message_factory=build_install_message,
        event_emitter=migration_mod.orchestrator_emitter(log),
        failure_threshold=0.2,
        challenge_message_factory=build_challenge_message,
        signature_parser=lambda response: parse_signature(response.data),
        skip_offline=True,
    )
    orch.start_migration(wave_size=3, canary_count=2, target_mode="pqc")
    for _ in range(2000):
        if orch.get_migration_status().is_terminal:
            break
        await asyncio.sleep(0.005)
    status = orch.get_migration_status()
    for sid in ours:   # the sessions end normally after the migration
        log.emit("transaction_ended", sid, outcome="success", transaction_id=f"tx{sid}",
                 seq_no=1, power_w=0.0, energy_wh=1.0, offline=False,
                 applied_to_live_state=True, trigger_reason="StopAuthorized")
    log.close()

    d.harness("stage6_n7_pqc.jsonl", time.time(), "fleet_snapshot",
              snapshot=_snapshot(fleet.states(), status, connected), **H)
    for sid, st in stations.items():
        d.harness("stage6_n7_pqc.jsonl", time.time(), "station_finished", station_id=sid,
                  ok=True, crashed=False, connect_ms=[1.0],
                  pq_key_installed=st.pq.is_migrated, pq_algorithm=st.pq.algorithm,
                  pq_installs=st.pq.installs, pq_challenges_signed=st.pq.challenges_signed, **H)
    d.harness("stage6_n7_pqc.jsonl", time.time() + 0.05, "run_finished", **H)
    d.write()

    # -- what the orchestrator itself says ------------------------------
    assert status.phase == MigrationPhase.ROLLED_BACK
    assert (status.migrated, status.incompatible, status.pending, status.rolled_back) == (6, 1, 1, 2)
    assert sum(1 for st in stations.values() if st.pq.challenges_signed == 1) == 6

    # -- C6 must say exactly the same ------------------------------------
    results = run.build(None, d.logs)
    slot = results["slots"]["stage6|n7|pqc|plain"]
    e3 = slot["e3"]
    assert e3["final_counts"] == {"pending": 1, "in_progress": 0, "migrated": 6,
                                  "rolled_back": 2, "incompatible": 1}
    assert e3["controller_counts"] == {"total_stations": 10, "pending": 1, "in_progress": 0,
                                       "migrated": 6, "rolled_back": 2, "incompatible": 1}
    assert e3["controller_sum_violations"] == 0
    assert e3["pq_checks"]["passed"] == 6 and e3["pq_checks"]["rejected"] == 0
    assert e3["verification"] == "authenticated"
    assert e3["deferred"] == {"count": 1, "station_ids": ["CP0008"]}
    assert e3["rollbacks"] == 1 and e3["phase"] == "rolled_back"
    assert e3["agent_view"] == {"available": True, "migrated_but_no_key": []}
    assert e3["charging"]["sessions_disturbed"] == 0
    assert slot["e5"]["pq_passed"] == 6
    assert slot["trust"]["status"] in ("pass", "info"), slot["trust"]