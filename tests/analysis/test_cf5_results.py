"""
C-F5 — the final results page: E4 certificate rows, the E3 halt and the key
rotation. Track C (tests). One happy-path test per feature.

Rotation lines are shaped exactly as Track B's B-F2 writes them
(origin/TrackB 5bcf1e5, idmanager/orchestrator.py): the run is announced
with migration_started / wave_* / migration_completed carrying kind
"rotation"; one rotation_completed or rotation_failed line per charger with
{station, wave_id, old_key_id, new_key_id, detail}; key checks with trigger
"rotation". All through Track A's emitter: no top-level station_id,
source "orchestrator".
"""

from __future__ import annotations

import sys
import types

from analysis import check, collect, match
from analysis.measures import e1_handshake, e3_migration, e4_sizes, overview
from tests.analysis.fakes import T0, Diaries


def _matched(d: Diaries, name: str):
    events, logs = d.write()
    return match.match_run(collect.read_harness_file(logs / name).runs[0],
                           collect.read_server_diary(events))


O = dict(source="orchestrator", mode="hybrid", run_id="srv1")


def rotation_lines(d: Diaries, ids: list[str], *, at: float, fail: set[str] = frozenset()) -> None:
    R = dict(kind="rotation", **O)
    d.server(at, "migration_started", None, migration_id="r1", target_mode="hybrid",
             total=len(ids), wave_size=2, canary_count=1, **R)
    d.server(at, "wave_started", None, wave_id=0, is_canary=True, size=len(ids), **R)
    for k, sid in enumerate(ids):
        t = at + 0.1 + 0.05 * k
        ok = sid not in fail
        d.server(t, "rotation_started", None, station=sid, wave_id=0,
                 old_key_id=f"old{k:013d}", new_key_id=f"new{k:013d}", algorithm="ML-DSA-44", **O)
        d.server(t + 0.01, "connection_attempt", None, transition="pq_auth", station=sid, wave_id=0,
                 result="success" if ok else "rejected", detail="", duration_ms=30.0 + k,
                 algorithm="ML-DSA-44", key_id=f"new{k:013d}", trigger="rotation", **O)
        d.server(t + 0.02, "rotation_completed" if ok else "rotation_failed", None, station=sid,
                 wave_id=0, old_key_id=f"old{k:013d}", new_key_id=f"new{k:013d}",
                 detail=None if ok else "signature did not verify", **O)
    d.server(at + 1.0, "wave_completed", None, wave_id=0, migrated=len(ids) - len(fail),
             failed=len(fail), incompatible=0, deferred=0, **R)
    d.server(at + 1.0, "migration_completed", None, migrated=len(ids) - len(fail), incompatible=0, **R)


# -- E4 ---------------------------------------------------------------------------


def _fake_pq_x509(monkeypatch, *, available: bool, rows=None, boom: bool = False) -> None:
    mod = types.ModuleType("crypto.pq_x509")
    mod.available = lambda: available

    def size_rows():
        if boom:
            raise RuntimeError("cannot build")
        return rows or []

    mod.size_rows = size_rows
    monkeypatch.setitem(sys.modules, "crypto.pq_x509", mod)


def test_e4_adds_track_bs_ml_dsa_certificate_rows(monkeypatch):
    row = e4_sizes._row("post-quantum certificate", "ML-DSA-44 chain (leaf + root)", 7800, 10700,
                        e4_sizes.CHAIN_LIMIT, "ML-DSA-44")
    _fake_pq_x509(monkeypatch, available=True, rows=[row])
    out = e4_sizes.measure()
    got = [r for r in out["rows"] if r["label"] == "ML-DSA-44 chain (leaf + root)"]
    assert got == [row] and got[0]["within_limit"] is False   # over 10,000: a real finding, reported as is


def test_e4_says_why_when_the_certificate_rows_are_missing(monkeypatch):
    _fake_pq_x509(monkeypatch, available=False)
    assert "ML-DSA certificate rows need cryptography 50+" in e4_sizes.measure()["notes"]
    _fake_pq_x509(monkeypatch, available=True, boom=True)
    assert any("RuntimeError: cannot build" in n for n in e4_sizes.measure()["notes"])
    monkeypatch.setitem(sys.modules, "crypto.pq_x509", None)      # module not on this branch
    notes = e4_sizes.measure()["notes"]
    assert any(n.startswith("ML-DSA certificate rows unavailable") for n in notes)


def test_e4_shows_contract_7_messages_not_installpqauth():
    labels = [r["label"] for r in e4_sizes.measure()["rows"] if r["group"] == "on the wire"]
    if labels:   # empty only without the post-quantum library
        assert "InstallPQAuth payload" not in labels
        assert "RequestPQEnrolment reply (public key)" in labels


# -- E3 halt ------------------------------------------------------------------------


def test_a_rollback_on_the_last_wave_is_not_called_a_halt(tmp_path):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="e3", n=8, mode="hybrid", migrate=True, charge_s=8)
    halt = e3_migration.measure(_matched(d, info["name"]))["halt"]
    assert halt == {"rolled_back_wave": 2, "never_attempted": 0, "shown": False}


def test_a_halt_mid_fleet_is_shown(tmp_path):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="e3halt", n=8, mode="hybrid", migrate=True, charge_s=8)
    ids = info["ids"]
    # Last snapshot: wave 1 rolled back, CP0005-CP0008 never attempted (L09 profile shape).
    states = {**{s: "migrated" for s in ids[:2]}, **{s: "rolled_back" for s in ids[2:4]},
              **{s: "pending" for s in ids[4:]}}
    snapshot = {
        "run_id": "srv1", "crypto_mode": "hybrid",
        "stations": [{"station_id": s, "connection_state": "connected", "boot_accepted": True,
                      "migration_state": st} for s, st in states.items()],
        "migration": {"phase": "rolled_back", "target_mode": "hybrid", "total_stations": 8,
                      "pending": 4, "in_progress": 0, "migrated": 2, "rolled_back": 2, "incompatible": 0,
                      "waves": [{"wave_id": 0, "is_canary": True, "station_ids": ids[:2], "phase": "completed",
                                 "migrated_count": 2, "failed_count": 0},
                                {"wave_id": 1, "is_canary": False, "station_ids": ids[2:4],
                                 "phase": "rolled_back", "migrated_count": 0, "failed_count": 2}]},
    }
    d.harness(info["name"], T0 + 8.5, "fleet_snapshot", snapshot=snapshot, run_id="run1",
              experiment="e3halt", n=8, mode="hybrid", tls=True)
    e3 = e3_migration.measure(_matched(d, info["name"]))
    assert e3["halt"] == {"rolled_back_wave": 1, "never_attempted": 4, "shown": True}


# -- E3 rotation --------------------------------------------------------------------


def test_a_rotation_run_is_reported_and_judged_by_its_own_checks(tmp_path):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="e3rot", n=4, mode="hybrid", charge_s=8)
    rotation_lines(d, info["ids"], at=T0 + 2.0, fail={"CP0004"})
    m = _matched(d, info["name"])
    e3 = e3_migration.measure(m)
    assert e3["kind"] == "rotation"
    rot = e3["rotation"]
    assert (rot["completed"], rot["failed"]) == (3, 1)
    assert rot["checks"]["total"] == 4 and rot["checks"]["passed"] == 3
    assert {k["station_id"]: (k["old_key_id"][:3], k["new_key_id"][:3], k["ok"]) for k in rot["keys"]}["CP0004"] \
        == ("old", "new", False)
    assert e3["pq_checks"]["total"] == 0                     # never mixed into the migration's numbers
    assert e3["verification"] == "authenticated"            # every rotated charger proved its new key
    trust = check.check_run(m, overview.measure(m), e1_handshake.measure(m), e3)
    assert "key_installed_not_authenticated" not in {i["code"] for i in trust["issues"]}


def test_a_migration_then_a_rotation_in_one_run_keeps_them_apart(tmp_path):
    d = Diaries(tmp_path)
    info = d.fleet_run(experiment="e3", n=8, mode="hybrid", migrate=True, charge_s=8)
    rotation_lines(d, info["ids"][:4], at=T0 + 5.0)
    e3 = e3_migration.measure(_matched(d, info["name"]))
    assert e3["kind"] == "migration"
    assert e3["pq_checks"]["total"] == 4                     # the migration's own 4, as before
    assert e3["rotation"]["completed"] == 4 and e3["rotation"]["failed"] == 0
